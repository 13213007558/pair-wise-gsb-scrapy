from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

from scrapy.http import Request, Response, TextResponse
from scrapy.linkextractors import LinkExtractor
from scrapy.spidermiddlewares.depth import DepthMiddleware
from scrapy.spiders import Spider
from scrapy.utils.asyncgen import as_async_generator, collect_asyncgen
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path
    from typing import Any, ClassVar

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


def _response_at_depth(url: str, depth: int) -> Response:
    response = Response(url)
    response.request = Request(url, meta={"depth": depth})
    return response


def _unsupported_callback(response: Response) -> None:
    """A callback that Request.to_dict() cannot serialize, as it is not
    a spider method."""


@pytest.fixture
def shortest_crawler() -> Crawler:
    return get_crawler(Spider, {"DEPTH_LIMIT": 3, "DEPTH_SHORTEST_PATH": True})


@pytest.fixture
def shortest_mw(shortest_crawler: Crawler) -> DepthMiddleware:
    return build_from_crawler(DepthMiddleware, shortest_crawler)


def test_shortest_path_disabled_by_default(mw: DepthMiddleware) -> None:
    assert mw._shortest_path is None


def test_shortest_path_tracking(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    state = shortest_mw._shortest_path
    assert state is not None
    response = _response_at_depth("https://example.com/", 0)

    out = list(
        shortest_mw.process_spider_output(response, [Request("https://example.com/a")])
    )

    assert len(out) == 1
    fingerprinter = shortest_crawler.request_fingerprinter
    assert response.request is not None
    fp_root = fingerprinter.fingerprint(response.request)
    fp_a = fingerprinter.fingerprint(Request("https://example.com/a"))
    assert state.min_depth == {fp_a: 1}
    assert state.outputs[fp_root] == [(1, Request("https://example.com/a").to_dict())]


def test_shortest_path_dont_filter_not_recorded(
    shortest_mw: DepthMiddleware,
) -> None:
    state = shortest_mw._shortest_path
    assert state is not None
    response = _response_at_depth("https://example.com/", 0)

    out = list(
        shortest_mw.process_spider_output(
            response, [Request("https://example.com/a", dont_filter=True)]
        )
    )

    assert len(out) == 1
    assert state.outputs == {}
    assert state.min_depth == {}


def test_shortest_path_unserializable_callback_not_recorded(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    state = shortest_mw._shortest_path
    assert state is not None
    response = _response_at_depth("https://example.com/", 0)
    request = Request("https://example.com/a", callback=_unsupported_callback)

    out = list(shortest_mw.process_spider_output(response, [request]))

    assert len(out) == 1
    assert state.outputs == {}
    fp_a = shortest_crawler.request_fingerprinter.fingerprint(
        Request("https://example.com/a")
    )
    assert state.min_depth == {fp_a: 1}


def test_shortest_path_improve_queued(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    shortest_crawler.engine = MagicMock()
    state = shortest_mw._shortest_path
    assert state is not None
    # x is discovered at depth 1
    list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/", 0),
            [Request("https://example.com/x")],
        )
    )

    # but it gets downloaded at depth 3, so its output y exceeds DEPTH_LIMIT
    out = list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/x", 3),
            [Request("https://example.com/y")],
        )
    )

    assert out == []
    # its outputs are re-expanded at the lower discovered depth instead
    shortest_crawler.engine.crawl.assert_called_once()
    replayed = shortest_crawler.engine.crawl.call_args[0][0]
    assert replayed.url == "https://example.com/y"
    assert replayed.meta["depth"] == 2


def test_shortest_path_replay_cascade() -> None:
    crawler = get_crawler(
        Spider,
        {"DEPTH_LIMIT": 3, "DEPTH_PRIORITY": 1, "DEPTH_SHORTEST_PATH": True},
    )
    mw = build_from_crawler(DepthMiddleware, crawler)
    crawler.engine = MagicMock()
    state = mw._shortest_path
    assert state is not None
    fingerprint = crawler.request_fingerprinter.fingerprint
    fp_a = fingerprint(Request("https://example.com/a"))
    fp_b = fingerprint(Request("https://example.com/b"))
    fp_c = fingerprint(Request("https://example.com/c"))
    fp_d = fingerprint(Request("https://example.com/d"))
    state.outputs[fp_a] = [(1, Request("https://example.com/b").to_dict())]
    state.outputs[fp_b] = [
        (1, Request("https://example.com/c").to_dict()),
        (3, Request("https://example.com/d").to_dict()),
    ]
    state.min_depth[fp_b] = 5
    state.min_depth[fp_c] = 5

    state._replay(fp_a, 1)

    # b was already downloaded, so it is not scheduled again, but its own
    # outputs are re-expanded at depths relative to its new depth
    crawler.engine.crawl.assert_called_once()
    replayed = crawler.engine.crawl.call_args[0][0]
    assert replayed.url == "https://example.com/c"
    assert replayed.meta["depth"] == 3
    assert replayed.priority == -3
    assert state.min_depth[fp_b] == 2
    assert state.min_depth[fp_c] == 3
    # d would exceed DEPTH_LIMIT even at the improved depth, so it is skipped
    assert fp_d not in state.min_depth


def test_shortest_path_state_persistence(tmp_path: Path) -> None:
    settings = {"DEPTH_SHORTEST_PATH": True, "JOBDIR": str(tmp_path)}
    crawler = get_crawler(Spider, settings)
    mw = build_from_crawler(DepthMiddleware, crawler)
    state = mw._shortest_path
    assert state is not None
    state.min_depth[b"fp"] = 2
    state.outputs[b"fp"] = [(1, {"url": "https://example.com/a"})]

    state._save()

    crawler2 = get_crawler(Spider, settings)
    mw2 = build_from_crawler(DepthMiddleware, crawler2)
    state2 = mw2._shortest_path
    assert state2 is not None
    assert state2.min_depth == {b"fp": 2}
    assert state2.outputs == {b"fp": [(1, {"url": "https://example.com/a"})]}


def test_shortest_path_replay_on_rediscovery(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    shortest_crawler.engine = MagicMock()
    state = shortest_mw._shortest_path
    assert state is not None
    # x is discovered at depth 3 and downloaded, its output y exceeds
    # DEPTH_LIMIT
    list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/b", 2),
            [Request("https://example.com/x")],
        )
    )
    list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/x", 3),
            [Request("https://example.com/y")],
        )
    )
    shortest_crawler.engine.crawl.assert_not_called()

    # when x is discovered again at a lower depth, its outputs are
    # re-expanded at depths relative to the new depth
    list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/", 0),
            [Request("https://example.com/x")],
        )
    )

    shortest_crawler.engine.crawl.assert_called_once()
    replayed = shortest_crawler.engine.crawl.call_args[0][0]
    assert replayed.url == "https://example.com/y"
    assert replayed.meta["depth"] == 2

    # a later discovery at a higher depth does nothing
    shortest_crawler.engine.crawl.reset_mock()
    list(
        shortest_mw.process_spider_output(
            _response_at_depth("https://example.com/b", 2),
            [Request("https://example.com/x")],
        )
    )
    shortest_crawler.engine.crawl.assert_not_called()


def test_shortest_path_replay_keeps_lower_child_depth(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    shortest_crawler.engine = MagicMock()
    state = shortest_mw._shortest_path
    assert state is not None
    fingerprint = shortest_crawler.request_fingerprinter.fingerprint
    fp_a = fingerprint(Request("https://example.com/a"))
    fp_z = fingerprint(Request("https://example.com/z"))
    state.outputs[fp_a] = [(1, Request("https://example.com/z").to_dict())]
    state.min_depth[fp_z] = 0

    state._replay(fp_a, 2)

    # z was already discovered at a lower depth, so its tracked depth does
    # not change; the re-scheduled copy is dropped by the dupe filter
    shortest_crawler.engine.crawl.assert_called_once()
    assert state.min_depth[fp_z] == 0


def test_shortest_path_replay_depth_reset(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    shortest_crawler.engine = MagicMock()
    state = shortest_mw._shortest_path
    assert state is not None
    response = _response_at_depth("https://example.com/", 5)
    list(
        shortest_mw.process_spider_output(
            response,
            [Request("https://example.com/r", meta={"depth_reset": True})],
        )
    )
    assert response.request is not None
    fp_root = shortest_crawler.request_fingerprinter.fingerprint(response.request)

    state._replay(fp_root, 2)

    # a depth_reset request keeps depth 0 when replayed
    shortest_crawler.engine.crawl.assert_called_once()
    replayed = shortest_crawler.engine.crawl.call_args[0][0]
    assert replayed.meta["depth"] == 0
    assert "depth_reset" not in replayed.meta


@coroutine_test
async def test_shortest_path_process_spider_output_async(
    shortest_mw: DepthMiddleware, shortest_crawler: Crawler
) -> None:
    state = shortest_mw._shortest_path
    assert state is not None
    response = _response_at_depth("https://example.com/", 0)

    out = await collect_asyncgen(
        shortest_mw.process_spider_output_async(
            response, as_async_generator([Request("https://example.com/a")])
        )
    )

    assert len(out) == 1
    fp_a = shortest_crawler.request_fingerprinter.fingerprint(
        Request("https://example.com/a")
    )
    assert state.min_depth == {fp_a: 1}
    state._save()  # no-op without JOBDIR


class ItemRecorderPipeline:
    items: ClassVar[list[Any]] = []

    def process_item(self, item: Any, spider: Spider) -> Any:
        self.items.append(item)
        return item


class ShortestPathSpider(MockServerSpider):
    name = "shortest-path-graph"

    def __init__(self, start_url: str, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.start_urls = [start_url]
        self.visited: list[str] = []

    def parse(self, response: Response) -> Iterator[Any]:
        assert isinstance(response, TextResponse)
        self.visited.append(response.url)
        if b"<!-- item -->" in response.body:
            yield {"url": response.url}
        for link in LinkExtractor().extract_links(response):
            yield Request(link.url, callback=self.parse)


def _raw_page_url(mockserver: MockServer, body: str) -> str:
    raw = (
        f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n{body}"
    )
    return mockserver.url("/raw?" + urlencode({"raw": raw}))


def _site_graph(mockserver: MockServer, start_order: str) -> dict[str, str]:
    """Build a site graph on the mock server:

    - ``start`` links to ``a`` and ``p``.
    - ``a`` links to ``b``, which links to ``x``: the long path to ``x``.
    - ``p`` links directly to ``x``: the short path to ``x``.
    - ``x`` links to ``y`` and yields an item.

    With ``DEPTH_LIMIT=3``, ``y`` is only within reach when ``x`` is
    discovered through the short path. *start_order* controls which branch
    is downloaded first: with the default LIFO scheduler queues,
    ``CONCURRENT_REQUESTS=1`` and ``SCRAPER_SLOT_MAX_ACTIVE_SIZE=0``
    (which keeps the next request from being dequeued before the outputs
    of the current response are scheduled), the branch linked last from
    ``start`` is downloaded first.
    """
    pages = {"y": _raw_page_url(mockserver, "<html><body>y</body></html>")}
    pages["x"] = _raw_page_url(
        mockserver,
        '<html><body><!-- item --><a href="' + pages["y"] + '">y</a></body></html>',
    )
    pages["b"] = _raw_page_url(
        mockserver, '<html><body><a href="' + pages["x"] + '">x</a></body></html>'
    )
    pages["a"] = _raw_page_url(
        mockserver, '<html><body><a href="' + pages["b"] + '">b</a></body></html>'
    )
    pages["p"] = _raw_page_url(
        mockserver, '<html><body><a href="' + pages["x"] + '">x</a></body></html>'
    )
    if start_order == "long_first":
        links = [pages["p"], pages["a"]]
    else:
        links = [pages["a"], pages["p"]]
    pages["start"] = _raw_page_url(
        mockserver,
        '<html><body><a href="'
        + links[0]
        + '">1</a><a href="'
        + links[1]
        + '">2</a></body></html>',
    )
    return pages


class TestShortestPathCrawl:
    @pytest.fixture(autouse=True)
    def _reset_items(self) -> None:
        ItemRecorderPipeline.items = []

    def _settings(self, **overrides: Any) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "CONCURRENT_REQUESTS": 1,
            "DEPTH_LIMIT": 3,
            "DEPTH_SHORTEST_PATH": True,
            "ITEM_PIPELINES": {ItemRecorderPipeline: 1},
            "SCRAPER_SLOT_MAX_ACTIVE_SIZE": 0,
        }
        settings.update(overrides)
        return settings

    async def _crawl(
        self, mockserver: MockServer, start_order: str, **overrides: Any
    ) -> tuple[ShortestPathSpider, dict[str, str]]:
        pages = _site_graph(mockserver, start_order)
        crawler = get_crawler(ShortestPathSpider, self._settings(**overrides))
        await crawler.crawl_async(start_url=pages["start"], mockserver=mockserver)
        assert isinstance(crawler.spider, ShortestPathSpider)
        return crawler.spider, pages

    @coroutine_test
    async def test_long_path_first(self, mockserver: MockServer) -> None:
        spider, pages = await self._crawl(mockserver, "long_first")

        # the long path completes first, but y is still crawled, after the
        # short branch discovers x at a lower depth
        assert spider.visited == [
            pages[name] for name in ("start", "a", "b", "x", "p", "y")
        ]
        assert ItemRecorderPipeline.items == [{"url": pages["x"]}]

    @coroutine_test
    async def test_short_path_first(self, mockserver: MockServer) -> None:
        spider, pages = await self._crawl(mockserver, "short_first")

        # the short path completes first, so the same pages are crawled
        assert spider.visited == [
            pages[name] for name in ("start", "p", "x", "y", "a", "b")
        ]
        assert ItemRecorderPipeline.items == [{"url": pages["x"]}]

    @coroutine_test
    async def test_disabled_long_path_first(self, mockserver: MockServer) -> None:
        spider, pages = await self._crawl(
            mockserver, "long_first", DEPTH_SHORTEST_PATH=False
        )

        assert set(spider.visited) == set(pages.values()) - {pages["y"]}

    @coroutine_test
    async def test_disabled_short_path_first(self, mockserver: MockServer) -> None:
        spider, pages = await self._crawl(
            mockserver, "short_first", DEPTH_SHORTEST_PATH=False
        )

        assert set(spider.visited) == set(pages.values())

    @coroutine_test
    async def test_jobdir_resume(self, mockserver: MockServer, tmp_path: Path) -> None:
        pages = _site_graph(mockserver, "long_first")
        settings = self._settings(JOBDIR=str(tmp_path), CLOSESPIDER_PAGECOUNT=4)
        crawler = get_crawler(ShortestPathSpider, settings)
        await crawler.crawl_async(start_url=pages["start"], mockserver=mockserver)
        assert isinstance(crawler.spider, ShortestPathSpider)
        # the crawl stops once x has been downloaded through the long path,
        # with the short branch (p) still in the scheduler queue
        assert set(crawler.spider.visited) == {
            pages[name] for name in ("start", "a", "b", "x")
        }
        assert (tmp_path / "requests.depths").is_file()

        crawler2 = get_crawler(ShortestPathSpider, settings)
        await crawler2.crawl_async(start_url=pages["start"], mockserver=mockserver)
        assert isinstance(crawler2.spider, ShortestPathSpider)
        # the resumed crawl explores the pending short branch and re-expands
        # the outputs of x at its newly-found lower depth
        # (start requests use dont_filter, so the start page is downloaded
        # again, but its outputs are dropped by the dupe filter)
        assert set(crawler2.spider.visited) == {
            pages[name] for name in ("start", "p", "y")
        }
        assert ItemRecorderPipeline.items == [{"url": pages["x"]}]
