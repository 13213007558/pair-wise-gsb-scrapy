"""
This module provides some useful functions for working with
scrapy.Request objects
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunparse, urlunsplit
from weakref import WeakKeyDictionary

from w3lib.url import canonicalize_url

from scrapy import Request, Spider
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.python import to_bytes, to_unicode

if TYPE_CHECKING:
    from collections.abc import Iterable

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.http.request import CookiesT, VerboseCookie

logger = logging.getLogger(__name__)


_fingerprint_cache: WeakKeyDictionary[Request, dict[tuple[Any, ...], bytes]] = (
    WeakKeyDictionary()
)

# Version of the request identity policy recorded in the job directory. Bump
# it whenever the fingerprint computation changes in a backward-incompatible
# way, so that resuming a job mixes only compatible fingerprints.
_FINGERPRINT_POLICY_VERSION = 1


def _filter_url_query(
    url: str,
    keep_query_params: tuple[str, ...] | None,
    ignore_query_params: tuple[str, ...] | None,
) -> str:
    """Return *url* with only the matching query parameters left.

    Parameter names are matched after percent-decoding. Repeated parameters
    and empty values are kept, and the relative order of the surviving
    parameters is preserved.
    """
    if not keep_query_params and not ignore_query_params:
        return url
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    keep = frozenset(keep_query_params) if keep_query_params else None
    ignore = frozenset(ignore_query_params or ())
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if (keep is None or name in keep) and name not in ignore
        ]
    )
    return urlunsplit(parsed._replace(query=query))


def fingerprint(
    request: Request,
    *,
    include_headers: Iterable[bytes | str] | None = None,
    keep_fragments: bool = False,
    keep_query_params: Iterable[str] | None = None,
    ignore_query_params: Iterable[str] | None = None,
    scope: str | None = None,
) -> bytes:
    """
    Return the request fingerprint.

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

    Use keep_query_params or ignore_query_params to restrict which URL query
    parameters take part in the fingerprint, for example to ignore tracking
    parameters. Parameter names are matched after percent-decoding; repeated
    parameters and empty values are preserved. Filtering only affects the
    fingerprint, the request itself is sent unmodified.

    Use scope to tell apart requests that are equivalent for one caller but
    must be tracked separately, for example requests made on behalf of
    different accounts. The scope is only a string provided by the caller; it
    is never derived from cookies, headers or request meta automatically.
    """
    processed_include_headers: tuple[bytes, ...] | None = None
    if include_headers:
        processed_include_headers = tuple(
            to_bytes(h.lower()) for h in sorted(include_headers)
        )
    verbatim_url = bool(request.meta.get("verbatim_url"))
    effective_keep_fragments = keep_fragments and not verbatim_url
    cache = _fingerprint_cache.setdefault(request, {})
    processed_keep_query_params = (
        tuple(sorted(keep_query_params)) if keep_query_params else None
    )
    processed_ignore_query_params = (
        tuple(sorted(ignore_query_params)) if ignore_query_params else None
    )
    cache_key = (
        processed_include_headers,
        effective_keep_fragments,
        verbatim_url,
        processed_keep_query_params,
        processed_ignore_query_params,
        scope,
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
        if verbatim_url:
            url = request.url
        else:
            url = canonicalize_url(request.url, keep_fragments=keep_fragments)
        url = _filter_url_query(
            url, processed_keep_query_params, processed_ignore_query_params
        )
        fingerprint_data = {
            "method": to_unicode(request.method),
            "url": url,
            "body": (request.body or b"").hex(),
            "headers": headers,
        }
        if scope is not None:
            fingerprint_data["scope"] = scope
        fingerprint_json = json.dumps(fingerprint_data, sort_keys=True)
        cache[cache_key] = hashlib.sha1(  # noqa: S324
            fingerprint_json.encode()
        ).digest()
    return cache[cache_key]


class RequestFingerprinterProtocol(Protocol):
    def fingerprint(self, request: Request) -> bytes: ...


class RequestFingerprinter:
    """Default fingerprinter.

    It takes into account a canonical version
    (:func:`w3lib.url.canonicalize_url`) of :attr:`request.url
    <scrapy.Request.url>` and the values of :attr:`request.method
    <scrapy.Request.method>` and :attr:`request.body
    <scrapy.Request.body>`, unless :reqmeta:`verbatim_url` is true for that
    request. It then generates an `SHA1 <https://en.wikipedia.org/wiki/SHA-1>`_
    hash.

    The :setting:`REQUEST_FINGERPRINT_INCLUDE_HEADERS`,
    :setting:`REQUEST_FINGERPRINT_KEEP_QUERY_PARAMS`,
    :setting:`REQUEST_FINGERPRINT_IGNORE_QUERY_PARAMS` and
    :setting:`REQUEST_FINGERPRINT_SCOPE` settings and the
    :reqmeta:`fingerprint_scope` request meta key refine which request
    attributes take part in the fingerprint.
    """

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def __init__(self, crawler: Crawler | None = None):
        self._fingerprint = fingerprint
        if crawler is not None:
            settings = crawler.settings
            self._include_headers: tuple[str, ...] = tuple(
                settings.getlist("REQUEST_FINGERPRINT_INCLUDE_HEADERS")
            )
            self._keep_query_params: tuple[str, ...] = tuple(
                settings.getlist("REQUEST_FINGERPRINT_KEEP_QUERY_PARAMS")
            )
            self._ignore_query_params: tuple[str, ...] = tuple(
                settings.getlist("REQUEST_FINGERPRINT_IGNORE_QUERY_PARAMS")
            )
            self._scope: str | None = settings.get("REQUEST_FINGERPRINT_SCOPE") or None
        else:
            self._include_headers = ()
            self._keep_query_params = ()
            self._ignore_query_params = ()
            self._scope = None

    def fingerprint(self, request: Request) -> bytes:
        scope = request.meta.get("fingerprint_scope", self._scope)
        return self._fingerprint(
            request,
            include_headers=self._include_headers or None,
            keep_query_params=self._keep_query_params or None,
            ignore_query_params=self._ignore_query_params or None,
            scope=scope,
        )

    def _identity_policy(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the configured request
        identity policy.

        The scope, which may identify an account, is hashed so that the
        summary never holds its value in clear text.
        """
        return {
            "version": _FINGERPRINT_POLICY_VERSION,
            "scope": (
                hashlib.sha1(to_bytes(self._scope)).hexdigest()  # noqa: S324
                if self._scope is not None
                else None
            ),
            "keep_query_params": sorted(self._keep_query_params),
            "ignore_query_params": sorted(self._ignore_query_params),
            "include_headers": sorted(
                header.lower() for header in self._include_headers
            ),
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
