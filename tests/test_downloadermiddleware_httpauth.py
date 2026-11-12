from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.downloadermiddlewares.httpauth import HttpAuthMiddleware
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from collections.abc import Sequence


def _build_mw(settings: dict[str, Any], spider: Spider) -> HttpAuthMiddleware:
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler(settings_dict=settings))
    if getattr(spider, "http_user", "") or getattr(spider, "http_pass", ""):
        with pytest.warns(ScrapyDeprecationWarning, match="spider attributes"):
            mw.spider_opened(spider)
    else:
        mw.spider_opened(spider)
    return mw


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        # Baseline
        ({}, None),
        # Settings.
        # HTTPAUTH_DOMAIN=None allows any domain.
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": None}},
            b"Basic Y3U6",
        ),
        (
            {"settings": {"HTTPAUTH_PASS": "cp", "HTTPAUTH_DOMAIN": None}},
            b"Basic OmNw",
        ),
        (
            {
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                }
            },
            b"Basic Y3U6Y3A=",
        ),
        # HTTPAUTH_DOMAIN=domain allows only that domain and subdomains.
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "a.example"}},
            b"Basic Y3U6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "a.example"},
            },
            b"Basic Y3U6",
        ),
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "b.example"}},
            None,
        ),
        # HTTPAUTH_DOMAIN must be set if HTTPAUTH_USER or HTTPAUTH_PASS are.
        ({"settings": {"HTTPAUTH_USER": "cu"}}, ValueError),
        ({"settings": {"HTTPAUTH_PASS": "cp"}}, ValueError),
        # Spider attributes.
        # http_auth_domain=None allows any domain.
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": None}},
            b"Basic c3U6",
        ),
        (
            {"spider_attributes": {"http_pass": "sp", "http_auth_domain": None}},
            b"Basic OnNw",
        ),
        (
            {
                "spider_attributes": {
                    "http_user": "su",
                    "http_pass": "sp",
                    "http_auth_domain": None,
                }
            },
            b"Basic c3U6c3A=",
        ),
        # http_auth_domain=domain allows only that domain and subdomains.
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": "a.example"}},
            b"Basic c3U6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "spider_attributes": {
                    "http_user": "su",
                    "http_auth_domain": "a.example",
                },
            },
            b"Basic c3U6",
        ),
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": "b.example"}},
            None,
        ),
        # http_auth_domain must be defined if http_user or http_pass are.
        ({"spider_attributes": {"http_user": "su"}}, AttributeError),
        # Spider attributes take priority over settings.
        (
            {
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                },
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            b"Basic c3U6",
        ),
        # Request.meta.
        ({"meta": {"http_user": "mu"}}, b"Basic bXU6"),
        ({"meta": {"http_pass": "mp"}}, b"Basic Om1w"),
        ({"meta": {"http_user": "mu", "http_pass": "mp"}}, b"Basic bXU6bXA="),
        # Request.meta["http_auth_domain"]=domain allows only that domain and
        # its subdomains.
        (
            {"meta": {"http_user": "mu", "http_auth_domain": "a.example"}},
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "meta": {"http_user": "mu", "http_auth_domain": "a.example"},
            },
            b"Basic bXU6",
        ),
        ({"meta": {"http_user": "mu", "http_auth_domain": "b.example"}}, None),
        # Without http_auth_domain, credentials from Request.meta are limited
        # to the origin of the request where they were first seen.
        #
        # Note: _auth_origin is not meant to be set by users, it is set the
        # first time a request is processed by the middleware. See
        # test_origin_setdefault.
        (
            {"meta": {"_auth_origin": "https://a.example", "http_user": "mu"}},
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://a.example:443",
                "meta": {"_auth_origin": "https://a.example", "http_user": "mu"},
            },
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://s.a.example",
                "meta": {"_auth_origin": "https://a.example", "http_user": "mu"},
            },
            None,
        ),
        ({"meta": {"_auth_origin": "http://a.example", "http_user": "mu"}}, None),
        ({"meta": {"_auth_origin": "https://a.example:1", "http_user": "mu"}}, None),
        ({"meta": {"_auth_origin": "https://b.example", "http_user": "mu"}}, None),
        # http_auth_domain takes priority over _auth_origin.
        (
            {
                "meta": {
                    "_auth_origin": "https://b.example",
                    "http_user": "mu",
                    "http_auth_domain": "a.example",
                }
            },
            b"Basic bXU6",
        ),
        # Takes priority over settings and spider attributes.
        (
            {
                "meta": {"http_user": "mu"},
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                },
            },
            b"Basic bXU6",
        ),
        (
            {
                "meta": {"http_user": "mu"},
                "spider_attributes": {
                    "http_user": "su",
                    "http_pass": "sp",
                    "http_auth_domain": None,
                },
            },
            b"Basic bXU6",
        ),
        # If the Authorization header is set, it is not modified.
        (
            {
                "headers": {"Authorization": "a"},
                "settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": None},
            },
            b"a",
        ),
        (
            {
                "headers": {"Authorization": "a"},
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            b"a",
        ),
        ({"headers": {"Authorization": "a"}, "meta": {"http_user": "mu"}}, b"a"),
        # If a non-HTTP request is received, nothing is done.
        (
            {
                "url": "ftp://example.com",
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            None,
        ),
        ({"url": "s3://example.com", "meta": {"http_user": "mu"}}, None),
    ],
)
def test_main(config: dict[str, Any], expected: bytes | type[Exception] | None) -> None:
    url = config.get("url", "https://a.example")
    headers = config.get("headers", {})
    meta = config.get("meta", {})
    settings = config.get("settings", {})
    spider_attributes = config.get("spider_attributes", {})

    class TestSpider(Spider):
        pass

    for k, v in spider_attributes.items():
        setattr(TestSpider, k, v)

    spider = TestSpider("foo")

    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            _build_mw(settings, spider)
        return

    mw = _build_mw(settings, spider)
    request = Request(url, headers=headers, meta=meta)
    assert mw.process_request(request) is None
    if expected is None:
        assert "Authorization" not in request.headers
    else:
        assert request.headers["Authorization"] == expected, repr(
            request.headers["Authorization"]
        )


@pytest.mark.parametrize(
    ("meta", "url", "output_value"),
    [
        ({}, "https://example.com/a", None),
        ({"http_user": "a", "_auth_origin": "foo"}, "https://example.com/a", "foo"),
        ({"http_user": "a"}, "https://example.com/a", "https://example.com"),
        ({"http_user": "a"}, "http://example.com/a", "http://example.com"),
        ({"http_user": "a"}, "https://example.com:443/a", "https://example.com"),
        ({"http_user": "a"}, "http://example.com:80/a", "http://example.com"),
        ({"http_user": "a"}, "https://example.com:80/a", "https://example.com:80"),
        ({"http_user": "a"}, "http://example.com:443/a", "http://example.com:443"),
        ({"http_user": "a"}, "https://example.com:1234/a", "https://example.com:1234"),
        ({"http_user": "a"}, "http://example.com:1234/a", "http://example.com:1234"),
        # No origin is tracked when the allowed domain is explicit.
        (
            {"http_user": "a", "http_auth_domain": "example.com"},
            "https://example.com/a",
            None,
        ),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "a"}])
def test_origin_setdefault(
    meta: dict[str, Any], url: str, output_value: str | None, headers: dict[str, str]
) -> None:
    """When request.meta is used for authorization, an _auth_origin meta key is
    defined on the request if not defined already."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request(url, headers=headers, meta=meta)
    assert mw.process_request(request) is None
    if output_value is None:
        assert "_auth_origin" not in request.meta
    else:
        assert request.meta["_auth_origin"] == output_value


async def _follow_redirects(
    settings: dict[str, Any],
    request: Request,
    locations: dict[str, str],
) -> list[tuple[str, bytes | None]]:
    """Run the full default downloader middleware chain, following redirect
    requests the way the engine does, and report the Authorization header that
    reaches the (in-memory) downloader on every hop."""
    crawler = get_crawler(Spider, settings)
    crawler.spider = crawler._create_spider("foo")
    manager = build_from_crawler(DownloaderMiddlewareManager, crawler)

    wire: list[tuple[str, bytes | None]] = []

    async def download_func(current: Request) -> Response:
        wire.append((current.url, current.headers.get("Authorization")))
        location = locations.get(current.url)
        if location is None:
            return Response(current.url, status=200)
        return Response(current.url, status=302, headers={"Location": location})

    for _ in range(len(locations) + 1):
        result = await manager.download_async(download_func, request)
        if not isinstance(result, Request):
            break
        request = result
    return wire


@pytest.mark.parametrize(
    ("settings", "request_kwargs", "locations", "expected"),
    [
        # Per-request credentials without an explicit domain are pinned to the
        # origin of the first request: a same-origin redirect keeps them, a
        # cross-origin one loses them and the middleware does not re-add them.
        (
            {},
            {"meta": {"http_user": "mu", "http_pass": "mp"}},
            {"https://a.example/1": "https://a.example/2"},
            [
                ("https://a.example/1", b"Basic bXU6bXA="),
                ("https://a.example/2", b"Basic bXU6bXA="),
            ],
        ),
        (
            {},
            {"meta": {"http_user": "mu", "http_pass": "mp"}},
            {"https://a.example/1": "https://b.example/2"},
            [
                ("https://a.example/1", b"Basic bXU6bXA="),
                ("https://b.example/2", None),
            ],
        ),
        # A different port or scheme is a different origin, so the credentials
        # are not re-added either.
        (
            {},
            {"meta": {"http_user": "mu", "http_pass": "mp"}},
            {"https://a.example/1": "https://a.example:8443/2"},
            [
                ("https://a.example/1", b"Basic bXU6bXA="),
                ("https://a.example:8443/2", None),
            ],
        ),
        # Middleware-level credentials scoped to a domain follow every redirect
        # within that domain (including subdomains) and are dropped outside it.
        (
            {
                "HTTPAUTH_USER": "cu",
                "HTTPAUTH_PASS": "cp",
                "HTTPAUTH_DOMAIN": "a.example",
            },
            {},
            {
                "https://a.example/1": "https://sub.a.example/2",
                "https://sub.a.example/2": "https://b.example/3",
            },
            [
                ("https://a.example/1", b"Basic Y3U6Y3A="),
                ("https://sub.a.example/2", b"Basic Y3U6Y3A="),
                ("https://b.example/3", None),
            ],
        ),
        # With HTTPAUTH_DOMAIN=None the credentials are deliberately sent to
        # every domain, including cross-origin redirect targets.
        (
            {
                "HTTPAUTH_USER": "cu",
                "HTTPAUTH_PASS": "cp",
                "HTTPAUTH_DOMAIN": None,
            },
            {},
            {"https://a.example/1": "https://b.example/2"},
            [
                ("https://a.example/1", b"Basic Y3U6Y3A="),
                ("https://b.example/2", b"Basic Y3U6Y3A="),
            ],
        ),
        # An explicit Authorization header is preserved on a same-origin
        # redirect and stripped on a cross-origin one, with nothing re-added
        # when no credentials are configured.
        (
            {},
            {"headers": {"Authorization": "Bearer user-token"}},
            {"https://a.example/1": "https://a.example/2"},
            [
                ("https://a.example/1", b"Bearer user-token"),
                ("https://a.example/2", b"Bearer user-token"),
            ],
        ),
        (
            {},
            {"headers": {"Authorization": "Bearer user-token"}},
            {"https://a.example/1": "https://b.example/2"},
            [
                ("https://a.example/1", b"Bearer user-token"),
                ("https://b.example/2", None),
            ],
        ),
    ],
)
def test_authorization_on_redirect(
    settings: dict[str, Any],
    request_kwargs: dict[str, Any],
    locations: dict[str, str],
    expected: Sequence[tuple[str, bytes | None]],
) -> None:
    start_url = next(iter(locations))
    request = Request(start_url, **request_kwargs)
    wire = asyncio.run(_follow_redirects(settings, request, locations))
    assert wire == list(expected)