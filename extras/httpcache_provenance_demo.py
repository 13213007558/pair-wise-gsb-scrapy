"""Auditable authorization provenance of cached HTTP responses.

Run it directly to see what a callback observes for each cache path::

    python extras/httpcache_provenance_demo.py

The :class:`FakeDownloader` never opens a socket: it hands scripted responses
to the real :class:`~scrapy.downloadermiddlewares.httpcache.HttpCacheMiddleware`,
so the provenance attached to every response can be inspected deterministically.

A callback that needs real-time authorization (see :func:`strict_callback`)
rejects every response whose origin verification happened in the past, however
fresh the cache entry claims to be. The cache stays enabled throughout: the
contract is enforced from the provenance record rather than by disabling
caching.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from email.utils import formatdate
from time import time
from typing import TYPE_CHECKING

from twisted.internet.error import ConnectionLost

from scrapy.downloadermiddlewares.httpcache import HttpCacheMiddleware
from scrapy.extensions.httpcache import (
    CACHE_FRESH,
    CACHE_OFFLINE,
    CACHE_REVALIDATED,
    CACHE_STALE,
    CacheProvenance,
)
from scrapy.http import Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

URL = "http://photos.example.com/resource"
# A response is considered fresh for one second after each origin answer.
FRESH_SECONDS = 1


class AuthorizationError(Exception):
    """Raised when a response only carries historical authorization proof."""


@dataclass
class FakeDownloader:
    """Scripted download function standing in for the network.

    Each entry of *script* is the response (or exception) the next "download"
    must produce, so revalidations and offline failures need no real server.
    """

    script: list[Response | Exception]
    requests_seen: list[Request] = field(default_factory=list)

    async def __call__(self, request: Request) -> Response:
        self.requests_seen.append(request)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_date(offset_seconds: float = 0.0) -> str:
    return formatdate(time() + offset_seconds, usegmt=True)


def _origin_response() -> Response:
    return Response(
        URL,
        headers={
            "Date": _http_date(),
            "Cache-Control": f"max-age={FRESH_SECONDS}",
            "Last-Modified": _http_date(-3600),
        },
        body=b"protected resource",
    )


def _crawler(cache_dir: str) -> Crawler:
    crawler = get_crawler(
        Spider,
        {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": cache_dir,
            "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy",
            "HTTPCACHE_STORAGE": (
                "scrapy.extensions.httpcache.FilesystemCacheStorage"
            ),
        },
    )
    crawler.spider = crawler._create_spider("provenance-demo")
    crawler.stats.open_spider()
    return crawler


def strict_callback(request: Request) -> CacheProvenance:
    """Accept only answers the origin authorized for the current request.

    A first-hand response carries no provenance and is accepted: the origin
    answered the request itself. A cached response is accepted only when the
    origin revalidated it during this request; entries served on historical
    proof are rejected even when they are still fresh, and the storage time is
    never mistaken for an authorization time.
    """
    provenance = request.meta.get("cache_provenance")
    if provenance is None:
        now = time()
        return CacheProvenance(
            source=CACHE_REVALIDATED,
            stored_at=now,
            origin_verified_at=now,
        )
    if not provenance.revalidated_with_origin:
        raise AuthorizationError(
            f"Rejecting {provenance.source!r}: the last origin authorization "
            f"was at {provenance.origin_verified_at}; the storage time "
            f"{provenance.stored_at} is not an authorization time."
        )
    return provenance


async def _fetch(
    middleware: HttpCacheMiddleware,
    downloader: FakeDownloader,
    request: Request,
) -> Response:
    """Drive one request through the middleware the way the downloader does."""
    response = await middleware.process_request(request)
    if response is not None:
        return response
    try:
        downloaded = await downloader(request)
    except Exception as exception:
        recovered = middleware.process_exception(request, exception)
        assert recovered is not None
        return recovered
    return middleware.process_response(request, downloaded)


async def _prime_cache(
    middleware: HttpCacheMiddleware, downloader: FakeDownloader
) -> float:
    request = Request(URL)
    response = await _fetch(middleware, downloader, request)
    assert "cached" not in response.flags
    assert "cache_provenance" not in request.meta
    # The storage stamps the verification time when the first-hand response is
    # written; read the entry back to learn the value it recorded.
    assert middleware.crawler.spider
    stored_request = Request(URL)
    cached = middleware.storage.retrieve_response(
        middleware.crawler.spider, stored_request
    )
    assert cached is not None
    return stored_request.meta["cache_origin_verified_timestamp"]


async def run_demo() -> list[tuple[str, str, bool]]:
    scenarios: list[tuple[str, list[Response | Exception], str, bool, float]] = [
        # name, scripted downloads after priming, expected source, accepted?,
        # seconds to wait before the second request
        (
            "fresh hit",
            [],
            CACHE_FRESH,
            False,
            0.0,
        ),
        (
            "stale entry kept after a 5xx response",
            [Response(URL, status=503, headers={"Date": _http_date()})],
            CACHE_STALE,
            False,
            FRESH_SECONDS + 0.1,
        ),
        (
            "stale entry revalidated with a 304 response",
            [Response(URL, status=304, headers={"Date": _http_date()})],
            CACHE_REVALIDATED,
            True,
            FRESH_SECONDS + 0.1,
        ),
        (
            "offline fallback after a connection loss",
            [ConnectionLost("network unreachable")],
            CACHE_OFFLINE,
            False,
            FRESH_SECONDS + 0.1,
        ),
    ]

    results: list[tuple[str, str, bool]] = []
    for name, script, expected_source, expected_accepted, delay in scenarios:
        with tempfile.TemporaryDirectory() as cache_dir:
            crawler = _crawler(cache_dir)
            middleware = build_from_crawler(HttpCacheMiddleware, crawler)
            assert crawler.spider
            middleware.spider_opened(crawler.spider)
            downloader = FakeDownloader([_origin_response(), *script])

            verified_at_priming = await _prime_cache(middleware, downloader)
            await asyncio.sleep(delay)

            request = Request(URL)
            response = await _fetch(middleware, downloader, request)
            assert "cached" in response.flags

            provenance: CacheProvenance = request.meta["cache_provenance"]
            assert provenance.source == expected_source, (
                name,
                provenance,
                response.flags,
            )
            if expected_source == CACHE_REVALIDATED:
                # A 304 is an origin answer, so the authorization time moves.
                assert provenance.origin_verified_at > verified_at_priming
                assert provenance.stored_at >= provenance.origin_verified_at
            else:
                # No origin answer: the historical authorization time is kept,
                # however the entry was refreshed or recovered.
                assert provenance.origin_verified_at == verified_at_priming

            try:
                strict_callback(request)
            except AuthorizationError:
                accepted = False
            else:
                accepted = True
            assert accepted == expected_accepted, name
            results.append((name, provenance.source, accepted))

            middleware.spider_closed(crawler.spider)
            crawler.stats.close_spider()
    return results


def test_cache_provenance_paths() -> None:
    """The four cache paths expose the source a strict callback can audit."""
    results = asyncio.run(run_demo())
    assert [source for _, source, _ in results] == [
        CACHE_FRESH,
        CACHE_STALE,
        CACHE_REVALIDATED,
        CACHE_OFFLINE,
    ]
    assert [accepted for _, _, accepted in results] == [
        False,
        False,
        True,
        False,
    ]


def _main() -> None:
    results = asyncio.run(run_demo())
    width = max(len(name) for name, _, _ in results)
    for name, source, accepted in results:
        decision = "accepted" if accepted else "rejected"
        print(f"{name.ljust(width)}  source={source:<17} strict callback: {decision}")


if __name__ == "__main__":
    _main()
