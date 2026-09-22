from __future__ import annotations

import gzip
import hashlib
import logging
import pickle
import shutil
from datetime import timezone
from email.utils import parsedate_to_datetime
from importlib import import_module
from pathlib import Path
from time import time
from typing import IO, TYPE_CHECKING, Any, Concatenate, TypedDict, cast
from weakref import WeakKeyDictionary

from w3lib.http import headers_dict_to_raw, headers_raw_to_dict

from scrapy.http import Headers, Response
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.project import data_path
from scrapy.utils.python import to_bytes, to_unicode
from scrapy.utils.response import response_from_dict

if TYPE_CHECKING:
    import os
    from collections.abc import Callable
    from types import ModuleType

    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings
    from scrapy.spiders import Spider
    from scrapy.utils.request import RequestFingerprinterProtocol


logger = logging.getLogger(__name__)


_VaryKey = tuple[tuple[bytes, ...], ...]


class _VaryIndex(TypedDict):
    """Index of the cached variants of a request fingerprint.

    "vary" holds the field names of the Vary response header that the cached
    variants were stored under, and "variants" the vary key of each cached
    variant.
    """

    vary: tuple[bytes, ...]
    variants: list[_VaryKey]


def _vary_names(headers: Headers) -> tuple[bytes, ...] | None:
    """Return the sorted, lowercased field names of the Vary header of
    *headers*, or ``None`` if there is none."""
    names: set[bytes] = set()
    for line in headers.getlist(b"Vary"):
        names.update(name.strip().lower() for name in line.split(b","))
    names.discard(b"")
    if not names:
        return None
    return tuple(sorted(names))


def _vary_key(request: Request, names: tuple[bytes, ...]) -> _VaryKey:
    """Return the values of the *names* request headers.

    Two requests can share a cached response only if their vary keys are
    equal. A missing header and a header with an empty value have different
    keys, and headers with multiple lines keep all their values.
    """
    return tuple(tuple(request.headers.getlist(name)) for name in names)


def _variant_id(vary_key: _VaryKey) -> str:
    return hashlib.sha1(pickle.dumps(vary_key, protocol=4)).hexdigest()  # noqa: S324


def _loads_vary_index(data: bytes) -> _VaryIndex | None:
    """Return the vary index stored in *data*, or ``None`` if it is missing
    or unreadable, so that a corrupted index is handled as a cache miss."""
    try:
        index = pickle.loads(data)  # noqa: S301
    except Exception:
        return None
    if not isinstance(index, dict) or "vary" not in index or "variants" not in index:
        return None
    return cast("_VaryIndex", index)


class DummyPolicy:
    def __init__(self, settings: BaseSettings):
        self.ignore_schemes: list[str] = settings.getlist("HTTPCACHE_IGNORE_SCHEMES")
        self.ignore_http_codes: list[int] = [
            int(x) for x in settings.getlist("HTTPCACHE_IGNORE_HTTP_CODES")
        ]

    def should_cache_request(self, request: Request) -> bool:
        return urlparse_cached(request).scheme not in self.ignore_schemes

    def should_cache_response(self, response: Response, request: Request) -> bool:
        return response.status not in self.ignore_http_codes

    def is_cached_response_fresh(
        self, cachedresponse: Response, request: Request
    ) -> bool:
        return True

    def is_cached_response_valid(
        self, cachedresponse: Response, response: Response, request: Request
    ) -> bool:
        return True


class RFC2616Policy:
    MAXAGE = 3600 * 24 * 365  # one year

    def __init__(self, settings: BaseSettings):
        self.always_store: bool = settings.getbool("HTTPCACHE_ALWAYS_STORE")
        self.ignore_schemes: list[str] = settings.getlist("HTTPCACHE_IGNORE_SCHEMES")
        self._cc_parsed: WeakKeyDictionary[
            Request | Response, dict[bytes, bytes | None]
        ] = WeakKeyDictionary()
        self.ignore_response_cache_controls: list[bytes] = [
            to_bytes(cc)
            for cc in settings.getlist("HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS")
        ]

    def _parse_cachecontrol(self, r: Request | Response) -> dict[bytes, bytes | None]:
        if r not in self._cc_parsed:
            cch = r.headers.get(b"Cache-Control", b"")
            assert cch is not None
            parsed = parse_cachecontrol(cch)
            if isinstance(r, Response):
                for key in self.ignore_response_cache_controls:
                    parsed.pop(key, None)
            self._cc_parsed[r] = parsed
        return self._cc_parsed[r]

    def should_cache_request(self, request: Request) -> bool:
        if urlparse_cached(request).scheme in self.ignore_schemes:
            return False
        cc = self._parse_cachecontrol(request)
        # obey user-agent directive "Cache-Control: no-store"
        return b"no-store" not in cc

    def should_cache_response(self, response: Response, request: Request) -> bool:
        # What is cacheable - https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.9.1
        # Response cacheability - https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.4
        # Status code 206 is not included because cache can not deal with partial contents
        cc = self._parse_cachecontrol(response)
        # obey directive "Cache-Control: no-store"
        if b"no-store" in cc:
            return False
        # Never cache 304 (Not Modified) responses
        if response.status == 304:
            return False
        # Cache unconditionally if configured to do so
        if self.always_store:
            return True
        # Any hint on response expiration is good
        if b"max-age" in cc or b"Expires" in response.headers:
            return True
        # Firefox fallbacks this statuses to one year expiration if none is set
        if response.status in {300, 301, 308}:
            return True
        # Other statuses without expiration requires at least one validator
        if response.status in {200, 203, 401}:
            return b"Last-Modified" in response.headers or b"ETag" in response.headers
        # Any other is probably not eligible for caching
        # Makes no sense to cache responses that does not contain expiration
        # info and can not be revalidated
        return False

    def is_cached_response_fresh(
        self, cachedresponse: Response, request: Request
    ) -> bool:
        cc = self._parse_cachecontrol(cachedresponse)
        ccreq = self._parse_cachecontrol(request)
        if b"no-cache" in cc or b"no-cache" in ccreq:
            return False

        now = time()
        freshnesslifetime = self._compute_freshness_lifetime(
            cachedresponse, request, now
        )
        currentage = self._compute_current_age(cachedresponse, request, now)

        reqmaxage = self._get_max_age(ccreq)
        if reqmaxage is not None:
            freshnesslifetime = min(freshnesslifetime, reqmaxage)

        if currentage < freshnesslifetime:
            return True

        if b"max-stale" in ccreq and b"must-revalidate" not in cc:
            # From RFC2616: "Indicates that the client is willing to
            # accept a response that has exceeded its expiration time.
            # If max-stale is assigned a value, then the client is
            # willing to accept a response that has exceeded its
            # expiration time by no more than the specified number of
            # seconds. If no value is assigned to max-stale, then the
            # client is willing to accept a stale response of any age."
            staleage = ccreq[b"max-stale"]
            if staleage is None:
                return True

            try:
                if currentage < freshnesslifetime + max(0, int(staleage)):
                    return True
            except ValueError:
                pass

        # Cached response is stale, try to set validators if any
        self._set_conditional_validators(request, cachedresponse)
        return False

    def is_cached_response_valid(
        self, cachedresponse: Response, response: Response, request: Request
    ) -> bool:
        # Use the cached response if the new response is a server error,
        # as long as the old response didn't specify must-revalidate.
        if response.status >= 500:
            cc = self._parse_cachecontrol(cachedresponse)
            if b"must-revalidate" not in cc:
                return True

        # Use the cached response if the server says it hasn't changed.
        return response.status == 304

    def _set_conditional_validators(
        self, request: Request, cachedresponse: Response
    ) -> None:
        if b"Last-Modified" in cachedresponse.headers:
            request.headers[b"If-Modified-Since"] = cachedresponse.headers[
                b"Last-Modified"
            ]

        if b"ETag" in cachedresponse.headers:
            request.headers[b"If-None-Match"] = cachedresponse.headers[b"ETag"]

    def _get_max_age(self, cc: dict[bytes, bytes | None]) -> int | None:
        try:
            return max(0, int(cc[b"max-age"]))  # type: ignore[arg-type]
        except (KeyError, ValueError):
            return None

    def _compute_freshness_lifetime(
        self, response: Response, request: Request, now: float
    ) -> float:
        # Reference nsHttpResponseHead::ComputeFreshnessLifetime
        # https://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#706
        cc = self._parse_cachecontrol(response)
        maxage = self._get_max_age(cc)
        if maxage is not None:
            return maxage

        # Parse date header or synthesize it if none exists
        date = rfc1123_to_epoch(response.headers.get(b"Date")) or now

        # Try HTTP/1.0 Expires header
        if b"Expires" in response.headers:
            expires = rfc1123_to_epoch(response.headers[b"Expires"])
            # When parsing Expires header fails RFC 2616 section 14.21 says we
            # should treat this as an expiration time in the past.
            return max(0, expires - date) if expires else 0

        # Fallback to heuristic using last-modified header
        # This is not in RFC but on Firefox caching implementation
        lastmodified = rfc1123_to_epoch(response.headers.get(b"Last-Modified"))
        if lastmodified and lastmodified <= date:
            return (date - lastmodified) / 10

        # This request can be cached indefinitely
        if response.status in {300, 301, 308}:
            return self.MAXAGE

        # Insufficient information to compute freshness lifetime
        return 0

    def _compute_current_age(
        self, response: Response, request: Request, now: float
    ) -> float:
        # Reference nsHttpResponseHead::ComputeCurrentAge
        # https://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#658
        currentage: float = 0
        # If Date header is not set we assume it is a fast connection, and
        # clock is in sync with the server
        date = rfc1123_to_epoch(response.headers.get(b"Date")) or now
        if now > date:
            currentage = now - date

        if b"Age" in response.headers:
            try:
                age = int(response.headers[b"Age"])  # type: ignore[arg-type]
                currentage = max(currentage, age)
            except ValueError:
                pass

        return currentage


class DbmCacheStorage:
    def __init__(self, settings: BaseSettings):
        self.cachedir: str = data_path(settings["HTTPCACHE_DIR"], createdir=True)
        self.expiration_secs: int = settings.getint("HTTPCACHE_EXPIRATION_SECS")
        self.dbmodule: ModuleType = import_module(settings["HTTPCACHE_DBM_MODULE"])
        self.db: Any = None  # the real type is private

    def open_spider(self, spider: Spider) -> None:
        dbpath = Path(self.cachedir, f"{spider.name}.db")
        self.db = self.dbmodule.open(str(dbpath), "c")

        logger.debug(
            "Using DBM cache storage in %(cachepath)s",
            {"cachepath": dbpath},
            extra={"spider": spider},
        )

        self._fingerprinter: RequestFingerprinterProtocol = (
            spider.crawler.request_fingerprinter
        )

    def close_spider(self, spider: Spider) -> None:
        self.db.close()

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        result = self._read_data(spider, request)
        if result is None:
            return None  # not cached
        data, ts = result
        request.meta["cache_timestamp"] = ts
        return response_from_dict(data)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        key = self._fingerprinter.fingerprint(request).hex()
        vary = _vary_names(response.headers)
        if vary is None or b"*" in vary:
            # A response without Vary replaces any cached variants, and a
            # "Vary: *" response cannot be reused, so it only invalidates.
            self._delete_cached_response(key)
            if vary is None:
                self.db[f"{key}_data"] = pickle.dumps(response.to_dict(), protocol=4)
                self.db[f"{key}_time"] = str(time())
            return
        index = self._read_vary_index(key)
        if index is None or index["vary"] != vary:
            # Variants cached under a different Vary set cannot be matched
            # against new requests.
            self._delete_cached_response(key)
            index = {"vary": vary, "variants": []}
        vary_key = _vary_key(request, vary)
        if vary_key not in index["variants"]:
            index["variants"].append(vary_key)
        variant_id = _variant_id(vary_key)
        self.db[f"{key}_data_{variant_id}"] = pickle.dumps(
            response.to_dict(), protocol=4
        )
        self.db[f"{key}_time_{variant_id}"] = str(time())
        self.db[f"{key}_vary"] = pickle.dumps(index, protocol=4)

    def _read_data(
        self, spider: Spider, request: Request
    ) -> tuple[dict[str, Any], float] | None:
        key = self._fingerprinter.fingerprint(request).hex()
        index = self._read_vary_index(key)
        if index is not None:
            vary_key = _vary_key(request, index["vary"])
            if vary_key not in index["variants"]:
                return None  # no matching variant
            variant_id = _variant_id(vary_key)
            return self._read_entry(
                f"{key}_data_{variant_id}", f"{key}_time_{variant_id}"
            )
        return self._read_entry(f"{key}_data", f"{key}_time")

    def _read_entry(
        self, data_key: str, time_key: str
    ) -> tuple[dict[str, Any], float] | None:
        db = self.db
        if time_key not in db:
            return None  # not found

        ts = float(db[time_key])
        if 0 < self.expiration_secs < time() - ts:
            return None  # expired

        data = cast("dict[str, Any]", pickle.loads(db[data_key]))  # noqa: S301
        return data, ts

    def _read_vary_index(self, key: str) -> _VaryIndex | None:
        index_key = f"{key}_vary"
        if index_key not in self.db:
            return None
        return _loads_vary_index(self.db[index_key])

    def _delete_cached_response(self, key: str) -> None:
        db = self.db
        if f"{key}_vary" not in db:
            for entry_key in (f"{key}_data", f"{key}_time"):
                if entry_key in db:
                    del db[entry_key]
            return
        prefix = f"{key}_"
        for raw_key in list(db.keys()):
            entry_key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            if entry_key.startswith(prefix):
                del db[entry_key]


class FilesystemCacheStorage:
    def __init__(self, settings: BaseSettings):
        self.cachedir: str = data_path(settings["HTTPCACHE_DIR"])
        self.expiration_secs: int = settings.getint("HTTPCACHE_EXPIRATION_SECS")
        self.use_gzip: bool = settings.getbool("HTTPCACHE_GZIP")
        # https://github.com/python/mypy/issues/10740
        self._open: Callable[
            Concatenate[str | os.PathLike[str], str, ...], IO[bytes]
        ] = (
            gzip.open if self.use_gzip else open  # type: ignore[assignment]
        )

    def open_spider(self, spider: Spider) -> None:
        logger.debug(
            "Using filesystem cache storage in %(cachedir)s",
            {"cachedir": self.cachedir},
            extra={"spider": spider},
        )

        self._fingerprinter = spider.crawler.request_fingerprinter

    def close_spider(self, spider: Spider) -> None:
        pass

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        """Return response if present in cache, or None otherwise."""
        rpath = Path(self._get_request_path(spider, request))
        index = self._read_vary_index(rpath)
        if index is not None:
            vary_key = _vary_key(request, index["vary"])
            if vary_key not in index["variants"]:
                return None  # no matching variant
            rpath = rpath / "variants" / _variant_id(vary_key)
        metadata = self._read_meta(rpath)
        if metadata is None:
            return None  # not cached
        with self._open(rpath / "response_body", "rb") as f:
            body = f.read()
        with self._open(rpath / "response_headers", "rb") as f:
            rawheaders = f.read()
        data = {
            "url": metadata["response_url"],
            "status": metadata["status"],
            "headers": headers_raw_to_dict(rawheaders),
            "body": body,
        }
        datapath = rpath / "response_data"
        if datapath.exists():
            with self._open(datapath, "rb") as f:
                data.update(pickle.load(f))  # noqa: S301
        request.meta["cache_timestamp"] = metadata["timestamp"]
        return response_from_dict(data)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        """Store the given response in the cache."""
        rpath = Path(self._get_request_path(spider, request))
        vary = _vary_names(response.headers)
        if vary is None or b"*" in vary:
            # A response without Vary replaces any cached variants, and a
            # "Vary: *" response cannot be reused, so it only invalidates.
            self._remove_cached_response(rpath)
            if vary is None:
                self._write_response(rpath, request, response)
            return
        index = self._read_vary_index(rpath)
        if index is None or index["vary"] != vary:
            # Variants cached under a different Vary set cannot be matched
            # against new requests.
            self._remove_cached_response(rpath)
            index = {"vary": vary, "variants": []}
        vary_key = _vary_key(request, vary)
        if vary_key not in index["variants"]:
            index["variants"].append(vary_key)
        self._write_response(
            rpath / "variants" / _variant_id(vary_key), request, response
        )
        with self._open(rpath / "vary_index", "wb") as f:
            pickle.dump(index, f, protocol=4)

    def _write_response(
        self, rpath: Path, request: Request, response: Response
    ) -> None:
        if not rpath.exists():
            rpath.mkdir(parents=True)
        metadata = {
            "url": request.url,
            "method": request.method,
            "status": response.status,
            "response_url": response.url,
            "timestamp": time(),
        }
        with self._open(rpath / "meta", "wb") as f:
            f.write(to_bytes(repr(metadata)))
        with self._open(rpath / "pickled_meta", "wb") as f:
            pickle.dump(metadata, f, protocol=4)
        with self._open(rpath / "response_headers", "wb") as f:
            f.write(headers_dict_to_raw(response.headers))
        with self._open(rpath / "response_body", "wb") as f:
            f.write(response.body)
        data = {
            key: value
            for key, value in response.to_dict().items()
            if key not in {"url", "status", "headers", "body"}
        }
        with self._open(rpath / "response_data", "wb") as f:
            pickle.dump(data, f, protocol=4)
        with self._open(rpath / "request_headers", "wb") as f:
            f.write(headers_dict_to_raw(request.headers))
        with self._open(rpath / "request_body", "wb") as f:
            f.write(request.body)

    def _get_request_path(self, spider: Spider, request: Request) -> str:
        key = self._fingerprinter.fingerprint(request).hex()
        return str(Path(self.cachedir, spider.name, key[0:2], key))

    def _read_vary_index(self, rpath: Path) -> _VaryIndex | None:
        try:
            with self._open(rpath / "vary_index", "rb") as f:
                return _loads_vary_index(f.read())
        except OSError:
            return None  # no vary index

    def _remove_cached_response(self, rpath: Path) -> None:
        shutil.rmtree(rpath / "variants", ignore_errors=True)
        for name in (
            "vary_index",
            "meta",
            "pickled_meta",
            "response_body",
            "response_headers",
            "response_data",
            "request_headers",
            "request_body",
        ):
            (rpath / name).unlink(missing_ok=True)

    def _read_meta(self, rpath: Path) -> dict[str, Any] | None:
        metapath = rpath / "pickled_meta"
        if not metapath.exists():
            return None  # not found
        mtime = metapath.stat().st_mtime
        if 0 < self.expiration_secs < time() - mtime:
            return None  # expired
        with self._open(metapath, "rb") as f:
            return cast("dict[str, Any]", pickle.load(f))  # noqa: S301


def parse_cachecontrol(header: bytes) -> dict[bytes, bytes | None]:
    """Parse Cache-Control header

    https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.9

    >>> parse_cachecontrol(b'public, max-age=3600') == {b'public': None,
    ...                                                 b'max-age': b'3600'}
    True
    >>> parse_cachecontrol(b'') == {}
    True

    """
    directives = {}
    for directive in header.split(b","):
        key, sep, val = directive.strip().partition(b"=")
        if key:
            directives[key.lower()] = val if sep else None
    return directives


def rfc1123_to_epoch(date_str: str | bytes | None) -> int | None:
    # The HTTP time format (which is how we use this function) is actually in
    # RFC 7231 §7.1.1.1, it refers to RFC 5322 and also requires support for
    # two obsolete formats (RFC 850 and asctime()). email.utils evidently
    # supports all three.
    if date_str is None:
        return None
    try:
        date_str = to_unicode(date_str, encoding="ascii")
        date = parsedate_to_datetime(date_str)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return int(date.timestamp())
    except Exception:
        return None
