"""
This module provides some useful functions for working with
scrapy.Request objects
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import SplitResult, unquote_plus, urlsplit, urlunparse, urlunsplit
from weakref import WeakKeyDictionary

from w3lib.url import canonicalize_url

from scrapy import Request, Spider
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.python import global_object_name, to_bytes, to_unicode

if TYPE_CHECKING:
    from collections.abc import Iterable

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.http.request import CookiesT, VerboseCookie

logger = logging.getLogger(__name__)


_fingerprint_cache: WeakKeyDictionary[
    Request,
    dict[
        tuple[
            tuple[bytes, ...] | None,
            bool,
            bool,
            tuple[str, ...] | None,
            tuple[str, ...] | None,
            bytes | None,
        ],
        bytes,
    ],
] = WeakKeyDictionary()


_FINGERPRINT_POLICY_VERSION = 1


def _decode_query_param_name(query_part: str) -> str:
    return unquote_plus(query_part.split("=", 1)[0])


def _filter_query_params(
    url: str,
    *,
    keep_query_params: tuple[str, ...] | None,
    ignore_query_params: tuple[str, ...] | None,
) -> str:
    if keep_query_params is None and ignore_query_params is None:
        return url
    parts: SplitResult = urlsplit(url)
    query_tokens = re.split(r"([;&])", parts.query)
    query_parts = query_tokens[::2]
    separators = [None, *query_tokens[1::2]]
    if keep_query_params is not None:
        keep = set(keep_query_params)
        kept_parts = [
            (part, separator)
            for part, separator in zip(query_parts, separators, strict=True)
            if _decode_query_param_name(part) in keep
        ]
    else:
        assert ignore_query_params is not None
        ignore = set(ignore_query_params)
        kept_parts = [
            (part, separator)
            for part, separator in zip(query_parts, separators, strict=True)
            if _decode_query_param_name(part) not in ignore
        ]
    if kept_parts:
        query = kept_parts[0][0] + "".join(
            f"{separator}{part}" for part, separator in kept_parts[1:]
        )
    else:
        query = ""
    return str(
        urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    )


def fingerprint(
    request: Request,
    *,
    include_headers: Iterable[bytes | str] | None = None,
    keep_fragments: bool = False,
    keep_query_params: Iterable[bytes | str] | None = None,
    ignore_query_params: Iterable[bytes | str] | None = None,
    scope: bytes | str | None = None,
) -> bytes:
    """
    Return the request fingerprint.

    .. versionchanged:: VERSION
        Added the *keep_query_params*, *ignore_query_params* and *scope*
        parameters.

    The request fingerprint is a hash that uniquely identifies the resource the
    request points to. For example, take the following two urls:
    ``http://www.example.com/query?id=111&cat=222``,
    ``http://www.example.com/query?cat=222&id=111``.

    Even though those are two different URLs both point to the same resource
    and are equivalent (i.e. they should return the same response).

    Another example are cookies used to store session ids. Suppose the
    following page is only accessible to authenticated users:
    ``http://www.example.com/members/offers.html``.

    Lots of sites use a cookie to store the session id, which adds a random
    component to the HTTP Request and thus should be ignored when calculating
    the fingerprint.

    For this reason, request headers are ignored by default when calculating
    the fingerprint. If you want to include specific headers use the
    include_headers argument, which is a list of Request headers to include.

    Also, servers usually ignore fragments in urls when handling requests,
    so they are also ignored by default when calculating the fingerprint.
    If you want to include them, set the keep_fragments argument to True
    (for instance when handling requests with a headless browser).

    keep_fragments is not a substitute for rendering JavaScript to reach
    content loaded based on the URL fragment; see
    :ref:`topics-dynamic-content` for that instead.

    Use *keep_query_params* to fingerprint only the listed query parameters,
    or *ignore_query_params* to exclude the listed query parameters. Only one
    of these parameters can be used. Query parameter names are matched after
    percent-decoding, while repeated parameters, empty values and the usual
    canonicalization order are preserved.

    Use *scope* to add a caller-provided identity scope, such as an account
    identifier, to the fingerprint.
    """
    if keep_query_params is not None and ignore_query_params is not None:
        raise ValueError(
            "keep_query_params and ignore_query_params cannot be used together"
        )
    processed_include_headers: tuple[bytes, ...] | None = None
    if include_headers:
        processed_include_headers = tuple(
            to_bytes(h.lower()) for h in sorted(include_headers)
        )
    processed_keep_query_params: tuple[str, ...] | None = None
    if keep_query_params is not None:
        processed_keep_query_params = tuple(
            sorted(to_unicode(param) for param in keep_query_params)
        )
    processed_ignore_query_params: tuple[str, ...] | None = None
    if ignore_query_params is not None:
        processed_ignore_query_params = tuple(
            sorted(to_unicode(param) for param in ignore_query_params)
        )
    processed_scope = to_bytes(scope) if scope is not None else None
    verbatim_url = bool(request.meta.get("verbatim_url"))
    effective_keep_fragments = keep_fragments and not verbatim_url
    cache = _fingerprint_cache.setdefault(request, {})
    cache_key = (
        processed_include_headers,
        effective_keep_fragments,
        verbatim_url,
        processed_keep_query_params,
        processed_ignore_query_params,
        processed_scope,
    )
    if cache_key not in cache:
        # To decode bytes reliably (JSON does not support bytes), regardless of
        # character encoding, we use bytes.hex()
        headers: dict[str, list[str]] = {}
        if processed_include_headers:
            for header in processed_include_headers:
                if header in request.headers:
                    headers[header.hex()] = [
                        header_value.hex()
                        for header_value in request.headers.getlist(header)
                    ]
        url = _filter_query_params(
            request.url,
            keep_query_params=processed_keep_query_params,
            ignore_query_params=processed_ignore_query_params,
        )
        if not verbatim_url:
            url = canonicalize_url(url, keep_fragments=keep_fragments)
        fingerprint_data = {
            "method": to_unicode(request.method),
            "url": url,
            "body": (request.body or b"").hex(),
            "headers": headers,
        }
        if processed_scope is not None:
            fingerprint_data["scope"] = processed_scope.hex()
        fingerprint_json = json.dumps(fingerprint_data, sort_keys=True)
        cache[cache_key] = hashlib.sha1(  # noqa: S324
            fingerprint_json.encode()
        ).digest()
    return cache[cache_key]


class RequestFingerprinterProtocol(Protocol):
    def fingerprint(self, request: Request) -> bytes: ...


class RequestFingerprinter:
    """Default fingerprinter.

    .. versionchanged:: VERSION
        Added support for the request fingerprinting settings documented in
        :ref:`request-fingerprints`.

    It takes into account a canonical version
    (:func:`w3lib.url.canonicalize_url`) of :attr:`request.url
    <scrapy.Request.url>` and the values of :attr:`request.method
    <scrapy.Request.method>` and :attr:`request.body
    <scrapy.Request.body>`, unless :reqmeta:`verbatim_url` is true for that
    request. It then generates an `SHA1 <https://en.wikipedia.org/wiki/SHA-1>`_
    hash.
    """

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def __init__(self, crawler: Crawler | None = None):
        self._include_headers: list[bytes | str] = []
        self._keep_query_params: list[bytes | str] | None = None
        self._ignore_query_params: list[bytes | str] | None = None
        self._scope: bytes | str | None = None
        self._scope_meta_key: str | None = None
        if crawler is not None:
            settings = crawler.settings
            self._include_headers = settings.getlist(
                "REQUEST_FINGERPRINTER_INCLUDE_HEADERS"
            )
            keep_query_params = settings["REQUEST_FINGERPRINTER_KEEP_QUERY_PARAMS"]
            if keep_query_params is not None:
                self._keep_query_params = settings.getlist(
                    "REQUEST_FINGERPRINTER_KEEP_QUERY_PARAMS"
                )
            ignore_query_params = settings["REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS"]
            if ignore_query_params is not None:
                self._ignore_query_params = settings.getlist(
                    "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS"
                )
            if (
                self._keep_query_params is not None
                and self._ignore_query_params is not None
            ):
                raise ValueError(
                    "REQUEST_FINGERPRINTER_KEEP_QUERY_PARAMS and "
                    "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS cannot be used "
                    "together"
                )
            self._scope = settings["REQUEST_FINGERPRINTER_SCOPE"]
            self._scope_meta_key = settings["REQUEST_FINGERPRINTER_SCOPE_META_KEY"]
            if self._scope is not None and self._scope_meta_key is not None:
                raise ValueError(
                    "REQUEST_FINGERPRINTER_SCOPE and "
                    "REQUEST_FINGERPRINTER_SCOPE_META_KEY cannot be used together"
                )

    def fingerprint(self, request: Request) -> bytes:
        scope = self._scope
        if self._scope_meta_key is not None:
            if (
                self._scope_meta_key not in request.meta
                or request.meta[self._scope_meta_key] is None
            ):
                raise ValueError(
                    f"Request {request!r} does not define the "
                    f"{self._scope_meta_key!r} meta key required by "
                    "REQUEST_FINGERPRINTER_SCOPE_META_KEY"
                )
            scope = request.meta[self._scope_meta_key]
        return fingerprint(
            request,
            include_headers=self._include_headers,
            keep_query_params=self._keep_query_params,
            ignore_query_params=self._ignore_query_params,
            scope=scope,
        )

    @property
    def _fingerprint_policy(self) -> dict[str, Any]:
        """Return the request fingerprinting policy, for job directories."""
        scope_digest = None
        if self._scope is not None:
            scope_digest = hashlib.sha256(to_bytes(self._scope)).hexdigest()
        return {
            "version": _FINGERPRINT_POLICY_VERSION,
            "fingerprinter": global_object_name(type(self)),
            "include_headers": sorted(
                to_unicode(to_bytes(header).lower()) for header in self._include_headers
            ),
            "keep_query_params": (
                None
                if self._keep_query_params is None
                else sorted(to_unicode(param) for param in self._keep_query_params)
            ),
            "ignore_query_params": (
                None
                if self._ignore_query_params is None
                else sorted(to_unicode(param) for param in self._ignore_query_params)
            ),
            "scope_meta_key": self._scope_meta_key,
            "scope_setting_sha256": scope_digest,
        }


def request_httprepr(request: Request) -> bytes:
    """Return the raw HTTP representation (as bytes) of the given request.
    This is provided only for reference since it's not the actual stream of
    bytes that will be send when performing the request (that's controlled
    by Twisted).
    """
    parsed = urlparse_cached(request)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    s = to_bytes(request.method) + b" " + to_bytes(path) + b" HTTP/1.1\r\n"
    s += b"Host: " + to_bytes(parsed.hostname or b"") + b"\r\n"
    if request.headers:
        s += request.headers.to_string() + b"\r\n"
    s += b"\r\n"
    s += request.body
    return s


def referer_str(request: Request) -> str | None:
    """Return Referer HTTP header suitable for logging."""
    referrer = request.headers.get("Referer")
    if referrer is None:
        return referrer
    return to_unicode(referrer, errors="replace")


def request_from_dict(d: dict[str, Any], *, spider: Spider | None = None) -> Request:
    """Create a :class:`~scrapy.Request` object from a dict.

    If a spider is given, it will try to resolve the callbacks looking at the
    spider for methods with the same name.
    """
    request_cls: type[Request] = load_object(d["_class"]) if "_class" in d else Request
    kwargs = {key: value for key, value in d.items() if key in request_cls.attributes}
    if d.get("callback") and spider:
        kwargs["callback"] = _get_method(spider, d["callback"])
    if d.get("errback") and spider:
        kwargs["errback"] = _get_method(spider, d["errback"])
    return request_cls(**kwargs)


def _get_method(obj: Any, name: object) -> Any:
    """Helper function for request_from_dict"""
    name = str(name)
    try:
        return getattr(obj, name)
    except AttributeError:
        raise ValueError(f"Method {name!r} not found in: {obj}") from None


def _to_verbose_cookies(cookies: CookiesT) -> list[VerboseCookie]:
    """Return a list of verbose cookies from ``request.cookies``.

    The list of dicts form is returned as is, the dict one is converted first.
    """
    if isinstance(cookies, dict):
        return [{"name": k, "value": v} for k, v in cookies.items()]
    return cookies


def _decode_cookie(cookie: VerboseCookie, request: Request) -> dict[str, str] | None:
    """Return a dict with non-flag verbose cookie values converted to strings.

    ``name``, ``value``, ``path``, ``domain`` are included, ``secure`` isn't.
    """

    decoded = {}
    for key in ("name", "value", "path", "domain"):
        value = cookie.get(key)
        if value is None:
            if key in {"name", "value"}:
                logger.warning(
                    f"Invalid cookie found in request {request}:"
                    f" {cookie} ('{key}' is missing)"
                )
                return None
            continue
        if isinstance(value, (bool, float, int, str)):
            decoded[key] = str(value)
        else:
            assert isinstance(value, bytes)
            try:
                decoded[key] = value.decode("utf8")
            except UnicodeDecodeError:
                logger.warning(
                    f"Non UTF-8 encoded cookie found in request {request}: {cookie}",
                )
                decoded[key] = value.decode("latin1", errors="replace")
    return decoded


def request_to_curl(request: Request) -> str:
    """
    Converts a :class:`~scrapy.Request` object to a curl command.

    :param request: Request object to be converted
    :return: string containing the curl command
    """
    method = request.method

    data = f"--data-raw '{request.body.decode('utf-8')}'" if request.body else ""

    headers = " ".join(
        f"-H '{k.decode()}: {v[0].decode()}'" for k, v in request.headers.items()
    )

    url = request.url

    cookie_list: list[VerboseCookie] = _to_verbose_cookies(request.cookies)
    pairs = [
        f"{decoded['name']}={decoded['value']}"
        for c in cookie_list
        if (decoded := _decode_cookie(c, request)) is not None
    ]
    cookies = f"--cookie '{'; '.join(pairs)}'" if pairs else ""

    curl_cmd = f"curl -X {method} {url} {data} {headers} {cookies}".strip()
    return " ".join(curl_cmd.split())
