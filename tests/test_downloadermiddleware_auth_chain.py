"""End-to-end checks of the headers that actually reach the download handler.

These tests drive the whole downloader middleware chain with in-memory
responses and fake credentials only. A recording download function captures the
final headers of every request that would be sent, including requests produced
by redirect and retry middlewares. The async chain is driven with
:func:`asyncio.run`, so no asynchronous pytest plugin is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.core.engine import ExecutionEngine
from scrapy.downloadermiddlewares.httpauth import HttpAuthMiddleware
from scrapy.http import Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler

FAKE_USER = "user"
FAKE_PASS = "pass"
FAKE_AUTH = b"Basic dXNlcjpwYXNz"

PROXY_CREDENTIALS = b"Basic " + b"cHJveHl1c2VyOnByb3h5cHc="

Responder = Callable[[Request], Response]


def _response(
    request: Request, *, status: int = 200, location: str | None = None
) -> Response:
    headers = {"Location": location} if location is not None else {}
    return Response(request.url, status=status, headers=headers)


class _ChainHarness:
    """Build a real downloader middleware chain and record sent headers."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings
        self.sent: list[dict[bytes, list[bytes]]] = []

    async def run(
        self,
        request: Request,
        responder: Responder,
    ) -> Response:
        crawler = get_crawler(Spider, self._settings)
        crawler.spider = crawler._create_spider("foo")
        manager = build_from_crawler(DownloaderMiddlewareManager, crawler)
        crawler.engine = ExecutionEngine(crawler)
        await crawler.engine.open_spider_async()
        try:
            async def download_func(download_request: Request) -> Response:
                self.sent.append(
                    {
                        key: list(value)
                        for key, value in download_request.headers.items()
                    }
                )
                return responder(download_request)

            result: Response | Request = await manager.download_async(
                download_func, request
            )
            # The engine re-schedules requests returned by middlewares
            # (redirects, retries); follow them here until a response settles.
            while isinstance(result, Request):
                result = await manager.download_async(download_func, result)
        finally:
            await crawler.engine.close_spider_async()
        assert isinstance(result, Response)
        return result


def _settings(**overrides: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "HTTPAUTH_USER": FAKE_USER,
        "HTTPAUTH_PASS": FAKE_PASS,
        "HTTPAUTH_DOMAIN": "a.example",
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.httpauth.HttpAuthMiddleware": 300,
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": 550,
            "scrapy.downloadermiddlewares.redirect.RedirectMiddleware": 600,
            "scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware": 750,
        },
    }
    settings.update(overrides)
    return settings


def _run(
    settings: dict[str, Any], request: Request, responder: Responder
) -> _ChainHarness:
    harness = _ChainHarness(settings)
    asyncio.run(harness.run(request, responder))
    return harness


def test_credential_sent_to_allowed_origin() -> None:
    harness = _run(
        _settings(),
        Request("https://a.example/start"),
        lambda req: _response(req),
    )
    assert harness.sent[-1][b"Authorization"] == [FAKE_AUTH]


def test_credential_not_sent_to_other_origin() -> None:
    harness = _run(
        _settings(),
        Request("https://b.example/start"),
        lambda req: _response(req),
    )
    assert b"Authorization" not in harness.sent[-1]


def test_cross_site_redirect_then_back_to_origin() -> None:
    def responder(req: Request) -> Response:
        host = req.url.split("/")[2]
        if host.startswith("a.example") and "/start" in req.url:
            return _response(req, status=302, location="https://b.example/mid")
        if host.startswith("b.example"):
            return _response(req, status=302, location="https://a.example/end")
        return _response(req)

    harness = _run(_settings(), Request("https://a.example/start"), responder)

    assert len(harness.sent) == 3
    assert b"Authorization" in harness.sent[0]
    # The cross-site hop carries no origin credential.
    assert b"Authorization" not in harness.sent[1]
    # Back on the allowed origin the policy re-attaches the credential.
    assert b"Authorization" in harness.sent[2]
    assert harness.sent[0][b"Authorization"] == [FAKE_AUTH]
    assert harness.sent[2][b"Authorization"] == [FAKE_AUTH]


def test_cross_site_redirect_does_not_leak_credential() -> None:
    def responder(req: Request) -> Response:
        if req.url == "https://a.example/start":
            return _response(req, status=302, location="https://b.example/end")
        return _response(req)

    harness = _run(_settings(), Request("https://a.example/start"), responder)
    assert b"Authorization" in harness.sent[0]
    assert all(b"Authorization" not in headers for headers in harness.sent[1:])
    # Nothing credential-shaped reaches the cross-site request.
    assert FAKE_AUTH.decode() not in repr(harness.sent[1])


def test_explicit_authorization_header_is_preserved() -> None:
    explicit = b"Bearer explicit-token"
    harness = _run(
        _settings(),
        # No redirect: the explicit header reaches the handler unchanged even
        # though the origin is not one the middleware policy would bind to.
        Request("https://b.example/start", headers={"Authorization": explicit}),
        lambda req: _response(req),
    )
    assert harness.sent[-1][b"Authorization"] == [explicit]
    # The middleware never substitutes its own credential for the explicit one.
    assert FAKE_AUTH not in repr(harness.sent).encode()


def test_explicit_authorization_stripped_on_cross_site_redirect() -> None:
    explicit = b"Bearer explicit-token"

    def responder(req: Request) -> Response:
        if req.url == "https://b.example/start":
            return _response(req, status=302, location="https://other.example/end")
        return _response(req)

    harness = _run(
        _settings(),
        Request("https://b.example/start", headers={"Authorization": explicit}),
        responder,
    )
    first, second = harness.sent
    assert first[b"Authorization"] == [explicit]
    # The cross-site hop drops the explicit header and gains no replacement.
    assert b"Authorization" not in second
    assert explicit not in repr(second).encode()
    assert FAKE_AUTH.decode() not in repr(second)


def test_retry_keeps_authorization() -> None:
    attempts = {"n": 0}

    def responder(req: Request) -> Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _response(req, status=500)
        return _response(req)

    harness = _run(_settings(), Request("https://a.example/start"), responder)
    assert len(harness.sent) == 2
    assert all(headers.get(b"Authorization") == [FAKE_AUTH] for headers in harness.sent)


def test_retry_after_cross_site_redirect_keeps_no_credential() -> None:
    # Cross-site redirect to b.example, which then fails and is retried. The
    # retry must not recover the credential used on the original origin.
    attempts = {"b": 0}

    def responder(req: Request) -> Response:
        if req.url == "https://a.example/start":
            return _response(req, status=302, location="https://b.example/work")
        attempts["b"] += 1
        if attempts["b"] == 1:
            return _response(req, status=503)
        return _response(req)

    harness = _run(_settings(), Request("https://a.example/start"), responder)
    assert len(harness.sent) == 3
    assert b"Authorization" in harness.sent[0]
    assert all(b"Authorization" not in headers for headers in harness.sent[1:])


def test_proxy_and_origin_authorization_coexist() -> None:
    harness = _run(
        _settings(),
        Request(
            "https://a.example/start",
            meta={"proxy": "http://proxyuser:proxypw@proxy.example:3128"},
        ),
        lambda req: _response(req),
    )
    headers = harness.sent[-1]
    assert headers[b"Authorization"] == [FAKE_AUTH]
    assert headers[b"Proxy-Authorization"] == [PROXY_CREDENTIALS]


def test_proxy_auth_without_origin_credential() -> None:
    harness = _run(
        _settings(),
        Request(
            "https://b.example/start",
            meta={"proxy": "http://proxyuser:proxypw@proxy.example:3128"},
        ),
        lambda req: _response(req),
    )
    headers = harness.sent[-1]
    assert b"Authorization" not in headers
    assert headers[b"Proxy-Authorization"] == [PROXY_CREDENTIALS]


def test_proxy_auth_survives_cross_site_redirect_without_origin_leak() -> None:
    def responder(req: Request) -> Response:
        if req.url == "https://a.example/start":
            return _response(req, status=302, location="https://b.example/end")
        return _response(req)

    harness = _run(
        _settings(),
        Request(
            "https://a.example/start",
            meta={"proxy": "http://proxyuser:proxypw@proxy.example:3128"},
        ),
        responder,
    )
    first, second = harness.sent
    # The explicit proxy is kept on the redirect, the origin credential is not.
    assert first[b"Proxy-Authorization"] == [PROXY_CREDENTIALS]
    assert second[b"Proxy-Authorization"] == [PROXY_CREDENTIALS]
    assert b"Authorization" in first
    assert b"Authorization" not in second
    assert FAKE_AUTH.decode() not in repr(second)


def test_denied_request_keeps_no_binding_or_credential() -> None:
    """A request the policy denies stores no binding and no header, and its
    meta never receives any credential material."""
    auth_mw = build_from_crawler(HttpAuthMiddleware, get_crawler(Spider, _settings()))
    request = Request("https://b.example/start")
    assert auth_mw.process_request(request) is None
    assert request._trusted_auth_binding is None
    assert b"Authorization" not in request.headers
    assert FAKE_AUTH not in repr(request.meta).encode()
    assert all(not key.startswith("_auth") for key in request.meta)


def test_trusted_binding_is_not_serialized() -> None:
    auth_mw = build_from_crawler(HttpAuthMiddleware, get_crawler(Spider, _settings()))
    request = Request("https://a.example/start")
    auth_mw.process_request(request)
    assert request._trusted_auth_binding is not None
    assert all(not key.startswith("_auth") for key in request.to_dict()["meta"])
