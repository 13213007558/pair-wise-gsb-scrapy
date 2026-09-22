from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest

from scrapy.http import Request, Response
from scrapy.spidermiddlewares.depth import DepthMiddleware
from scrapy.spiders import Spider
from scrapy.utils.asyncgen import as_async_generator, collect_asyncgen
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator

    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector
    from tests.mockserver.http import MockServer


@pytest.fixture
def crawler() -> Crawler:
    return get_crawler(Spider, {"DEPTH_LIMIT": 1, "DEPTH_STATS_VERBOSE": True})


@pytest.fixture
def stats(crawler: Crawler) -> Generator[StatsCollector]:
    crawler.stats.open_spider()

    yield crawler.stats

    crawler.stats.close_spider()


@pytest.fixture
def mw(crawler: Crawler) -> DepthMiddleware:
    return build_from_crawler(DepthMiddleware, crawler)


def test_process_spider_output(mw: DepthMiddleware, stats: StatsCollector) -> None:
    req = Request("http://scrapytest.org")
    resp = Response("http://scrapytest.org")
    resp.request = req
    result = [Request("http://scrapytest.org")]

    out = list(mw.process_spider_output(resp, result))
    assert out == result

    rdc = stats.get_value("request_depth_count/1")
    assert rdc == 1

    req.meta["depth"] = 1

    out2 = list(mw.process_spider_output(resp, result))
    assert not out2

    rdm = stats.get_value("request_depth_max")
    assert rdm == 1


def test_depth_reset(mw: DepthMiddleware, stats: StatsCollector) -> None:
    resp = Response("https://example.com")
    resp.request = Request("https://example.com", meta={"depth": 5})
    result = [Request("https://example.com", meta={"depth_reset": True})]

    out = list(mw.process_spider_output(resp, result))

    assert out == result
    assert out[0].meta["depth"] == 0
    assert "depth_reset" not in out[0].meta
    assert stats.get_value("request_depth_count/0") == 1


def test_process_spider_output_no_response(
    mw: DepthMiddleware, stats: StatsCollector
) -> None:
    result = [Request("https://example.com")]

    out = list(mw.process_spider_output(None, result))
    assert out == result
    assert "depth" not in out[0].meta
    assert stats.get_value("request_depth_count/0") is None


@coroutine_test
async def test_process_spider_output_async_no_response(
    mw: DepthMiddleware, stats: StatsCollector
) -> None:
    result = [Request("https://example.com")]

    out = await collect_asyncgen(
        mw.process_spider_output_async(None, as_async_generator(result))
    )
    assert out == result
    assert "depth" not in out[0].meta
    assert stats.get_value("request_depth_count/0") is None


def test_ignored_logged_once(
    mw: DepthMiddleware, stats: StatsCollector, caplog: pytest.LogCaptureFixture
) -> None:
    resp = Response("http://example.com")
    resp.request = Request("http://example.com")
    resp.meta["depth"] = 1
    result = [Request(f"http://example.com/{i}") for i in range(3)]

    with caplog.at_level("DEBUG", logger="scrapy.spidermiddlewares.depth"):
        assert not list(mw.process_spider_output(resp, result))

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "http://example.com/0" in messages[0]
    assert stats.get_value("depth/request_ignored_count") == 3


def test_priority_and_non_verbose_stats() -> None:
    crawler = get_crawler(
        Spider,
        {"DEPTH_LIMIT": 0, "DEPTH_STATS_VERBOSE": False, "DEPTH_PRIORITY": 10},
    )
    crawler.stats.open_spider()
    try:
        mw = build_from_crawler(DepthMiddleware, crawler)
        resp = Response("http://toscrape.com")
        resp.request = Request("http://toscrape.com")
        resp.request.meta["depth"] = 2
        out = list(mw.process_spider_output(resp, [Request("http://toscrape.com")]))
        assert len(out) == 1
        # priority is decremented by depth * DEPTH_PRIORITY
        assert out[0].priority == -30
        assert out[0].meta["depth"] == 3
        # non-verbose stats don't track per-depth counts but still track the max
        assert crawler.stats.get_value("request_depth_count/3") is None
        assert crawler.stats.get_value("request_depth_max") == 3
    finally:
        crawler.stats.close_spider()


class DepthGraphSpider(MockServerSpider):
    """Crawl a fixed link graph, recording the depth of every visited page.

    *pages* maps a page name to the names of the pages it links to, in link
    order. Each page is served by the mock server at its own URL, so the
    crawl goes through the regular scheduler, dupefilter and downloader.
    Links to pages named in *reset_pages* are followed with
    ``depth_reset``.
    """

    name = "depth-graph"

    def __init__(
        self,
        *args: Any,
        pages: dict[str, list[str]],
        start_pages: tuple[str, ...] = ("deep",),
        reset_pages: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.pages = pages
        self.reset_pages = reset_pages
        assert self.mockserver
        self.start_urls = [self._page_url(page) for page in start_pages]
        self.visited: list[tuple[str, int]] = []

    def _page_url(self, page: str) -> str:
        assert self.mockserver
        return self.mockserver.url(f"/text?page={page}")

    def _request(self, page: str) -> Request:
        meta = {"depth_reset": True} if page in self.reset_pages else None
        return Request(self._page_url(page), meta=meta)

    def _visit(self, response: Response) -> str:
        page = parse_qs(urlparse(response.url).query)["page"][0]
        self.visited.append((page, response.meta["depth"]))
        return page


class SyncDepthGraphSpider(DepthGraphSpider):
    name = "sync-depth-graph"

    def parse(self, response: Response) -> Iterator[Request]:
        page = self._visit(response)
        for link in self.pages[page]:
            yield self._request(link)


class AsyncDepthGraphSpider(DepthGraphSpider):
    name = "async-depth-graph"

    async def parse(self, response: Response) -> AsyncIterator[Request]:
        page = self._visit(response)
        for link in self.pages[page]:
            yield self._request(link)


# deep -> a -> x -> y and home -> x, where home is reached from a with
# depth_reset, so that it is discovered at depth 0 only after x has already
# been seen at depth 2. With DEPTH_LIMIT=2, y is out of reach through x at
# depth 2, but not through x at depth 1.
GRAPH = {
    "deep": ["a"],
    "a": ["x", "home"],
    "x": ["y"],
    "home": ["x"],
    "y": [],
}

# Same graph with an additional link from x back to a, closing a cycle.
CYCLE_GRAPH = {
    "deep": ["a"],
    "a": ["x", "home"],
    "x": ["y", "a"],
    "home": ["x"],
    "y": [],
}

CRAWL_SETTINGS = {
    "CONCURRENT_REQUESTS": 1,
    "DEPTH_LIMIT": 2,
    "DUPEFILTER_CLASS": "scrapy.dupefilters.MinDepthRFPDupeFilter",
}

CRAWL_KWARGS: dict[str, Any] = {
    "pages": GRAPH,
    "start_pages": ("deep",),
    "reset_pages": ("home",),
}

VISITED_WITH_MIN_DEPTH = [
    ("a", 1),
    ("deep", 0),
    ("home", 0),
    # x is scheduled again at the strictly lower depth 1, so its callback
    # runs again and y stays within DEPTH_LIMIT.
    ("x", 1),
    ("x", 2),
    ("y", 2),
]


@pytest.mark.parametrize("spider_cls", [SyncDepthGraphSpider, AsyncDepthGraphSpider])
class TestMinDepthDupeFilterCrawl:
    @coroutine_test
    async def test_shallower_path_rediscovered(
        self, spider_cls: type[DepthGraphSpider], mockserver: MockServer
    ) -> None:
        crawler = get_crawler(spider_cls, CRAWL_SETTINGS)
        await crawler.crawl_async(mockserver=mockserver, **CRAWL_KWARGS)
        assert isinstance(crawler.spider, DepthGraphSpider)
        assert sorted(crawler.spider.visited) == VISITED_WITH_MIN_DEPTH
        assert crawler.stats.get_value("depth/request_ignored_count") == 1

    @coroutine_test
    async def test_cycle_does_not_reschedule(
        self, spider_cls: type[DepthGraphSpider], mockserver: MockServer
    ) -> None:
        crawler = get_crawler(spider_cls, CRAWL_SETTINGS)
        await crawler.crawl_async(
            mockserver=mockserver, **{**CRAWL_KWARGS, "pages": CYCLE_GRAPH}
        )
        assert isinstance(crawler.spider, DepthGraphSpider)
        # a is known at depth 1, so the cycle back to a never schedules it
        # again, and the crawl visits every page as in the acyclic graph.
        assert sorted(crawler.spider.visited) == VISITED_WITH_MIN_DEPTH
        assert crawler.stats.get_value("depth/request_ignored_count") == 2
        assert crawler.stats.get_value("dupefilter/filtered") == 1


@coroutine_test
async def test_default_dupefilter_loses_deeper_followups(
    mockserver: MockServer,
) -> None:
    """Without MinDepthRFPDupeFilter, the duplicate request for x at depth 1
    is filtered out and y is never crawled."""
    settings = {
        **CRAWL_SETTINGS,
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
    }
    crawler = get_crawler(SyncDepthGraphSpider, settings)
    await crawler.crawl_async(mockserver=mockserver, **CRAWL_KWARGS)
    assert isinstance(crawler.spider, DepthGraphSpider)
    assert sorted(crawler.spider.visited) == [
        ("a", 1),
        ("deep", 0),
        ("home", 0),
        ("x", 2),
    ]
    assert crawler.stats.get_value("dupefilter/filtered") == 1
