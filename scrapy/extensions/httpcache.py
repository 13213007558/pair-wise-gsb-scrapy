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
from typing import IO, TYPE_CHECKING, Any, Concatenate, cast
from weakref import WeakKeyDictionary

from w3lib.http import headers_dict_to_raw, headers_raw_to_dict

from scrapy.http import Response
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


# A variant index entry: (sorted Vary field names, values of those fields in
# the stored request, variant id). It maps requests to one of the response
# variants stored under a single request fingerprint.
_VariantEntry = tuple[tuple[bytes, ...], tuple[tuple[bytes, ...] | None, ...], str]


def _vary_fields(response: Response) -> frozenset[bytes] | None:
    """Return the lower-cased field names of the Vary header of *response*.

    Return ``None`` if the Vary header is ``*``, meaning that the response
    must never be served from the cache.
    """
    fields: set[bytes] = set()
    for header in response.headers.getlist(b"Vary"):
        for value in header.split(b","):
            name = value.strip().lower()
            if name == b"*":
                return None
            if name:
                fields.add(name)
    return frozenset(fields)


def _vary_values(
    request: Request, fields: tuple[bytes, ...]
) -> tuple[tuple[bytes, ...] | None, ...]:
    """Return the values of the header *fields* of *request*.

    A missing header is represented by ``None``, telling it apart from a
    header that is present but empty.
    """
    return tuple(tuple(request.headers.getlist(field)) or None for field in fields)


def _variant_id(
    fields: tuple[bytes, ...], values: tuple[tuple[bytes, ...] | None, ...]
) -> str:
    return hashlib.sha256(repr((fields, values)).encode()).hexdigest()


def _find_variant(index: list[_VariantEntry], request: Request) -> str | None:
    """Return the id of the indexed variant that matches *request*, if any."""
    for fields, values, variant_id in index:
        if _vary_values(request, fields) == values:
            return variant_id
    return None


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
        try:
            index = self._read_index(key)
        except Exception:
            index = None  # unreadable index, start over
        kept: list[_VariantEntry] = []
        for fields, values, variant_id in index or []:
            if _vary_values(request, fields) == values:
                # The new response supersedes this variant, e.g. when a
                # revalidation changed the Vary field set.
                self._delete_variant(key, variant_id)
            else:
                kept.append((fields, values, variant_id))
        # Remove entries written before variant support existed.
        for suffix in ("data", "time"):
            legacy_key = f"{key}_{suffix}"
            if legacy_key in self.db:
                del self.db[legacy_key]
        vary = _vary_fields(response)
        if vary is None:
            # A Vary: * response must never be served from the cache.
            self._write_index(key, kept)
            return
        fields = tuple(sorted(vary))
        values = _vary_values(request, fields)
        variant_id = _variant_id(fields, values)
        self.db[f"{key}_data_{variant_id}"] = pickle.dumps(
            response.to_dict(), protocol=4
        )
        self.db[f"{key}_time_{variant_id}"] = str(time())
        kept.append((fields, values, variant_id))
        self._write_index(key, kept)

    def _read_index(self, key: str) -> list[_VariantEntry] | None:
        ikey = f"{key}_index"
        if ikey not in self.db:
            return None
        return cast("list[_VariantEntry]", pickle.loads(self.db[ikey]))  # noqa: S301

    def _write_index(self, key: str, index: list[_VariantEntry]) -> None:
        self.db[f"{key}_index"] = pickle.dumps(index, protocol=4)

    def _delete_variant(self, key: str, variant_id: str) -> None:
        for suffix in ("data", "time"):
            vkey = f"{key}_{suffix}_{variant_id}"
            if vkey in self.db:
                del self.db[vkey]

    def _read_data(
        self, spider: Spider, request: Request
    ) -> tuple[dict[str, Any], float] | None:
        key = self._fingerprinter.fingerprint(request).hex()
        db = self.db
        index = self._read_index(key)
        if index is None:
            # Entry written before variant support existed. It can only be
            # reused if its response does not vary.
            tkey = f"{key}_time"
            if tkey not in db:
                return None  # not found
            ts = float(db[tkey])
            if 0 < self.expiration_secs < time() - ts:
                return None  # expired
            data = cast("dict[str, Any]", pickle.loads(db[f"{key}_data"]))  # noqa: S301
            if any(header.lower() == b"vary" for header in data.get("headers", {})):
                return None  # cannot tell which variant it is
            return data, ts
        variant_id = _find_variant(index, request)
        if variant_id is None:
            return None  # no variant matches this request
        tkey = f"{key}_time_{variant_id}"
        if tkey not in db:
            return None  # not found
        ts = float(db[tkey])
        if 0 < self.expiration_secs < time() - ts:
            return None  # expired
        data = cast("dict[str, Any]", pickle.loads(db[f"{key}_data_{variant_id}"]))  # noqa: S301
        return data, ts


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
        index = self._read_index(rpath)
        legacy = index is None
        if index is not None:
            variant_id = _find_variant(index, request)
            if variant_id is None:
                return None  # no variant matches this request
            rpath = rpath / "variants" / variant_id
        metadata = self._read_meta(rpath)
        if metadata is None:
            return None  # not cached
        response = self._read_response(rpath, metadata)
        if legacy and b"Vary" in response.headers:
            # Entries written before variant support existed cannot be
            # mapped to a variant, so they can only be reused if their
            # response does not vary.
            return None
        request.meta["cache_timestamp"] = metadata["timestamp"]
        return response

    def _read_response(self, rpath: Path, metadata: dict[str, Any]) -> Response:
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
        return response_from_dict(data)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        """Store the given response in the cache."""
        rpath = Path(self._get_request_path(spider, request))
        try:
            index = self._read_index(rpath)
        except Exception:
            index = None  # unreadable index, start over
        kept: list[_VariantEntry] = []
        for fields, values, variant_id in index or []:
            if _vary_values(request, fields) == values:
                # The new response supersedes this variant, e.g. when a
                # revalidation changed the Vary field set.
                shutil.rmtree(rpath / "variants" / variant_id, ignore_errors=True)
            else:
                kept.append((fields, values, variant_id))
        # Remove entries written before variant support existed.
        for name in (
            "meta",
            "pickled_meta",
            "response_headers",
            "response_body",
            "response_data",
            "request_headers",
            "request_body",
        ):
            (rpath / name).unlink(missing_ok=True)
        vary = _vary_fields(response)
        if vary is None:
            # A Vary: * response must never be served from the cache.
            self._write_index(rpath, kept)
            return
        fields = tuple(sorted(vary))
        values = _vary_values(request, fields)
        variant_id = _variant_id(fields, values)
        vpath = rpath / "variants" / variant_id
        vpath.mkdir(parents=True, exist_ok=True)
        metadata = {
            "url": request.url,
            "method": request.method,
            "status": response.status,
            "response_url": response.url,
            "timestamp": time(),
        }
        with self._open(vpath / "meta", "wb") as f:
            f.write(to_bytes(repr(metadata)))
        with self._open(vpath / "pickled_meta", "wb") as f:
            pickle.dump(metadata, f, protocol=4)
        with self._open(vpath / "response_headers", "wb") as f:
            f.write(headers_dict_to_raw(response.headers))
        with self._open(vpath / "response_body", "wb") as f:
            f.write(response.body)
        data = {
            key: value
            for key, value in response.to_dict().items()
            if key not in {"url", "status", "headers", "body"}
        }
        with self._open(vpath / "response_data", "wb") as f:
            pickle.dump(data, f, protocol=4)
        with self._open(vpath / "request_headers", "wb") as f:
            f.write(headers_dict_to_raw(request.headers))
        with self._open(vpath / "request_body", "wb") as f:
            f.write(request.body)
        kept.append((fields, values, variant_id))
        self._write_index(rpath, kept)

    def _read_index(self, rpath: Path) -> list[_VariantEntry] | None:
        ipath = rpath / "vary_index"
        if not ipath.exists():
            return None
        with self._open(ipath, "rb") as f:
            return cast("list[_VariantEntry]", pickle.load(f))  # noqa: S301

    def _write_index(self, rpath: Path, index: list[_VariantEntry]) -> None:
        rpath.mkdir(parents=True, exist_ok=True)
        with self._open(rpath / "vary_index", "wb") as f:
            pickle.dump(index, f, protocol=4)

    def _get_request_path(self, spider: Spider, request: Request) -> str:
        key = self._fingerprinter.fingerprint(request).hex()
        return str(Path(self.cachedir, spider.name, key[0:2], key))

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
