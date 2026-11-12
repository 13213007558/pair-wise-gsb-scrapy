"""
HTTP basic auth downloader middleware

See documentation in docs/topics/downloader-middleware.rst
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from w3lib.http import basic_auth_header

from scrapy import Request, Spider, signals
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.settings import SETTINGS_PRIORITIES
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.url import url_is_from_any_domain

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http import Response


_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
}


def _origin(request: Request) -> str:
    parsed_url = urlparse_cached(request)
    scheme = parsed_url.scheme
    netloc = (
        parsed_url.netloc
        if parsed_url.port != _DEFAULT_PORTS[scheme]
        else parsed_url.hostname
    )
    return f"{scheme}://{netloc}"


@dataclass(frozen=True, slots=True)
class AuthBinding:
    """A trusted, in-memory record that a credential was allowed for an origin.

    The record is created only by the configured binding policy and lives on
    the request outside of :attr:`~scrapy.Request.meta`, so it cannot be
    forged or copied through the public API. It stores the already-encoded
    authorization header rather than the raw credential.
    """

    origin: str
    authorization: bytes

    def is_for_origin(self, request: Request) -> bool:
        return urlparse_cached(request).scheme in _DEFAULT_PORTS and (
            _origin(request) == self.origin
        )


class HttpAuthPolicy:
    """Decide which credential, if any, a request is allowed to present.

    The policy is the single trusted source of the binding decision. Subclass
    it and point :setting:`HTTPAUTH_BINDING_POLICY` at the subclass to change
    how credentials are selected or bound. A decision that denies a request
    returns ``None`` and never touches the credential material.
    """

    def __init__(
        self,
        *,
        auth: bytes | None,
        domain: str | None,
        request_user: str,
        request_pass: str,
        request_domain: str | None,
    ) -> None:
        self._auth = auth
        self._domain = domain
        self._request_user = request_user
        self._request_pass = request_pass
        self._request_domain = request_domain

    def bind(self, request: Request) -> AuthBinding | None:
        """Return the binding granted to *request*, or ``None`` when no
        credential may be attached to it."""
        if urlparse_cached(request).scheme not in _DEFAULT_PORTS:
            return None

        # Per-request credentials take priority over the middleware ones.
        if self._request_user or self._request_pass:
            if self._request_domain is not None:
                if not url_is_from_any_domain(request, [self._request_domain]):
                    return None
            # Without an explicit domain the credential is pinned, through the
            # binding origin, to the origin for which it is first granted.
            return AuthBinding(
                _origin(request),
                basic_auth_header(self._request_user, self._request_pass),
            )

        if self._auth is not None and (
            not self._domain or url_is_from_any_domain(request, [self._domain])
        ):
            return AuthBinding(_origin(request), self._auth)
        return None


class HttpAuthMiddleware:
    """Set Basic HTTP Authorization header."""

    def __init__(self, policy_cls: type[HttpAuthPolicy]) -> None:
        self._policy_cls = policy_cls
        self._auth: bytes | None = None
        self._domain: str | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        policy_cls = load_object(crawler.settings["HTTPAUTH_BINDING_POLICY"])
        o = cls(policy_cls)
        usr = crawler.settings.get("HTTPAUTH_USER", "")
        pwd = crawler.settings.get("HTTPAUTH_PASS", "")
        if usr or pwd:
            domain_priority = crawler.settings.getpriority("HTTPAUTH_DOMAIN") or 0
            if domain_priority <= SETTINGS_PRIORITIES["default"]:
                raise ValueError(
                    "HTTPAUTH_DOMAIN must be set when HTTPAUTH_USER or HTTPAUTH_PASS "
                    "is configured. Set it to a domain (e.g. 'example.com') to restrict "
                    "credentials to that domain, or set it to None to send credentials "
                    "with all requests."
                )
            o._auth = basic_auth_header(usr, pwd)
            o._domain = crawler.settings.get("HTTPAUTH_DOMAIN")
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def spider_opened(self, spider: Spider) -> None:
        usr = getattr(spider, "http_user", "")
        pwd = getattr(spider, "http_pass", "")
        if usr or pwd:
            warnings.warn(
                "Use the HTTPAUTH_USER, HTTPAUTH_PASS, and HTTPAUTH_DOMAIN settings "
                "instead of the http_user, http_pass, and http_auth_domain spider "
                "attributes. Support for the spider attributes will be removed in a "
                "future version of Scrapy.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            self._auth = basic_auth_header(usr, pwd)
            self._domain = spider.http_auth_domain  # type: ignore[attr-defined]

    def _build_policy(self, request: Request) -> HttpAuthPolicy:
        return self._policy_cls(
            auth=self._auth,
            domain=self._domain,
            request_user=request.meta.get("http_user", ""),
            request_pass=request.meta.get("http_pass", ""),
            request_domain=request.meta.get("http_auth_domain"),
        )

    @_warn_spider_arg
    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Request | Response | None:
        if urlparse_cached(request).scheme not in _DEFAULT_PORTS:
            return None

        # An explicit Authorization header always wins and is managed by its
        # owner, so the middleware neither replaces nor rebinds it.
        if b"Authorization" in request.headers:
            return None

        binding = request._trusted_auth_binding
        if binding is None:
            binding = self._build_policy(request).bind(request)
            if binding is not None:
                request._set_trusted_auth_binding(binding)

        if binding is not None and binding.is_for_origin(request):
            request.headers.setdefault(b"Authorization", binding.authorization)
        return None
