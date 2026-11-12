from __future__ import annotations

from typing import Any

import pytest

from scrapy.downloadermiddlewares.httpauth import (
    AuthBinding,
    HttpAuthMiddleware,
    HttpAuthPolicy,
)
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler


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
        # Without http_auth_domain, meta credentials are pinned, through a
        # trusted binding, to the origin where they are first granted. The
        # pinning behaviour across requests is covered by the dedicated
        # trusted-binding tests below.
        ({"meta": {"http_user": "mu"}}, b"Basic bXU6"),
        # An explicit http_auth_domain decides on every request, so no origin
        # pinning is needed.
        (
            {
                "meta": {
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
    ("url", "expected_origin"),
    [
        ("https://example.com/a", "https://example.com"),
        ("http://example.com/a", "http://example.com"),
        ("https://example.com:443/a", "https://example.com"),
        ("http://example.com:80/a", "http://example.com"),
        ("https://example.com:80/a", "https://example.com:80"),
        ("http://example.com:443/a", "http://example.com:443"),
        ("https://example.com:1234/a", "https://example.com:1234"),
        ("http://example.com:1234/a", "http://example.com:1234"),
    ],
)
def test_binding_granted_for_origin(url: str, expected_origin: str) -> None:
    """A granted binding records the request origin and the encoded header."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request(url, meta={"http_user": "mu"})
    assert mw.process_request(request) is None
    binding = request._trusted_auth_binding
    assert binding is not None
    assert binding.origin == expected_origin
    assert binding.authorization == request.headers["Authorization"] == b"Basic bXU6"


def test_binding_absent_without_credentials() -> None:
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request("https://example.com/a")
    assert mw.process_request(request) is None
    assert request._trusted_auth_binding is None
    assert "Authorization" not in request.headers


def test_binding_denied_is_recordless_and_credential_free() -> None:
    """A request denied by the policy keeps no binding and no Authorization
    header, and nothing credential-shaped is exposed."""
    mw = build_from_crawler(
        HttpAuthMiddleware,
        get_crawler(
            settings_dict={"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "a.example"}
        ),
    )
    request = Request("https://b.example")
    assert mw.process_request(request) is None
    assert request._trusted_auth_binding is None
    assert "Authorization" not in request.headers
    assert "_auth_origin" not in request.meta


def test_binding_not_in_meta_or_dict() -> None:
    """The trusted binding is not reachable through meta or serialization."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request("https://example.com", meta={"http_user": "mu"})
    mw.process_request(request)
    assert request._trusted_auth_binding is not None
    assert all(not key.startswith("_auth") for key in request.meta)
    serialized = request.to_dict()
    assert all(not key.startswith("_auth") for key in serialized["meta"])


def test_binding_survives_same_origin_copy() -> None:
    """A retry (same-origin copy) keeps both the binding and the header."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request("https://example.com/a", meta={"http_user": "mu"})
    mw.process_request(request)
    retry = request.copy()
    assert retry._trusted_auth_binding is request._trusted_auth_binding
    assert mw.process_request(retry) is None
    assert retry.headers["Authorization"] == b"Basic bXU6"


@pytest.mark.parametrize(
    "new_url",
    [
        "https://other.example/a",
        "http://example.com/a",
        "https://example.com:8080/a",
    ],
)
def test_binding_dropped_on_cross_origin_replace(new_url: str) -> None:
    """A derived request for another origin loses the binding and is
    re-evaluated from scratch rather than reusing the old credential."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request("https://example.com/a", meta={"http_user": "mu"})
    mw.process_request(request)
    redirected = request.replace(url=new_url)
    assert redirected._trusted_auth_binding is None
    # The redirect middleware strips the copied Authorization header on an
    # origin change; the credential meta still travels with the request.
    redirected.headers.pop("Authorization", None)
    assert mw.process_request(redirected) is None
    binding = redirected._trusted_auth_binding
    assert binding is not None
    assert binding.origin != request._trusted_auth_binding.origin
    assert redirected.headers["Authorization"] == b"Basic bXU6"


def test_explicit_authorization_header_is_not_rebound() -> None:
    mw = build_from_crawler(
        HttpAuthMiddleware,
        get_crawler(settings_dict={"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": None}),
    )
    request = Request("https://example.com", headers={"Authorization": "Bearer token"})
    assert mw.process_request(request) is None
    assert request.headers["Authorization"] == b"Bearer token"
    assert request._trusted_auth_binding is None


def test_custom_policy() -> None:
    """An application can replace the binding policy through settings."""
    mw = build_from_crawler(
        HttpAuthMiddleware,
        get_crawler(
            settings_dict={
                "HTTPAUTH_BINDING_POLICY": (
                    "tests.test_downloadermiddleware_httpauth.DenyAllPolicy"
                )
            }
        ),
    )
    request = Request("https://example.com", meta={"http_user": "mu"})
    assert mw.process_request(request) is None
    assert request._trusted_auth_binding is None
    assert "Authorization" not in request.headers


class DenyAllPolicy(HttpAuthPolicy):
    def bind(self, request: Request) -> AuthBinding | None:
        return None
