from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.http import Request, Response
from scrapy.spidermiddlewares.depth import DepthMiddleware
from scrapy.spiders import Spider
from scrapy.utils.asyncgen import as_async_generator, collect_asyncgen
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator, Iterator
    from pathlib import Path
    from typing import Any

    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector


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


@coroutine_test
async def test_process_spider_output_async(
    mw: DepthMiddleware, stats: StatsCollector
) -> None:
    resp = Response("https://example.com")
    resp.request = Request("https://example.com")
    result = [Request("https://example.com")]

    out = await collect_asyncgen(
        mw.process_spider_output_async(resp, as_async_generator(result))
    )
    assert out == result
    assert out[0].meta["depth"] == 1
    assert stats.get_value("request_depth_count/1") == 1


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


def _response(url: str, depth: int) -> Response:
    response = Response(url)
    response.request = Request(url, meta={"depth": depth})
    return response


@pytest.fixture
def sd_crawler() -> Crawler:
    return get_crawler(Spider, {"DEPTH_LIMIT": 2, "DEPTH_SHORTEST_DISCOVERY": True})


@pytest.fixture
def sd_mw(sd_crawler: Crawler) -> Generator[DepthMiddleware]:
    sd_crawler.stats.open_spider()
    yield build_from_crawler(DepthMiddleware, sd_crawler)
    sd_crawler.stats.close_spider()


def test_default_mode_keeps_no_state(mw: DepthMiddleware) -> None:
    resp = _response("https://example.com/x", depth=0)
    out = list(mw.process_spider_output(resp, [Request("https://example.com/y")]))
    assert len(out) == 1
    assert not mw._min_depth
    assert not mw._outputs
    assert not mw._pending


def test_reexpand_on_shorter_discovery(
    sd_mw: DepthMiddleware, sd_crawler: Crawler
) -> None:
    resp_x = _response("https://example.com/x", depth=2)
    out = list(sd_mw.process_spider_output(resp_x, [Request("https://example.com/y")]))
    assert out == []  # depth 3 > DEPTH_LIMIT 2

    resp_a = _response("https://example.com/a", depth=0)
    rediscovery = Request("https://example.com/x")
    out = list(sd_mw.process_spider_output(resp_a, [rediscovery]))
    assert len(out) == 2
    assert out[0] is rediscovery
    assert out[0].meta["depth"] == 1
    assert out[1].url == "https://example.com/y"
    assert out[1].meta["depth"] == 2
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") == 1


@coroutine_test
async def test_reexpand_on_shorter_discovery_async(sd_mw: DepthMiddleware) -> None:
    resp_x = _response("https://example.com/x", depth=2)
    out = await collect_asyncgen(
        sd_mw.process_spider_output_async(
            resp_x, as_async_generator([Request("https://example.com/y")])
        )
    )
    assert out == []

    resp_a = _response("https://example.com/a", depth=0)
    out = await collect_asyncgen(
        sd_mw.process_spider_output_async(
            resp_a, as_async_generator([Request("https://example.com/x")])
        )
    )
    assert [r.url for r in out] == [
        "https://example.com/x",
        "https://example.com/y",
    ]
    assert out[1].meta["depth"] == 2


def test_no_reexpand_on_greater_depth(
    sd_mw: DepthMiddleware, sd_crawler: Crawler
) -> None:
    resp_x = _response("https://example.com/x", depth=1)
    out = list(sd_mw.process_spider_output(resp_x, [Request("https://example.com/y")]))
    assert len(out) == 1  # y at depth 2, within DEPTH_LIMIT

    resp_a = _response("https://example.com/a", depth=1)
    rediscovery = Request("https://example.com/x")
    out = list(sd_mw.process_spider_output(resp_a, [rediscovery]))
    # the rediscovery itself goes through, but being deeper than the first
    # discovery of x it does not re-expand its recorded outputs
    assert out == [rediscovery]
    assert rediscovery.meta["depth"] == 2
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") is None


def test_cycle_does_not_replay(sd_mw: DepthMiddleware, sd_crawler: Crawler) -> None:
    resp_a = _response("https://example.com/a", depth=0)
    out = list(sd_mw.process_spider_output(resp_a, [Request("https://example.com/b")]))
    assert len(out) == 1

    resp_b = _response("https://example.com/b", depth=1)
    cycle = Request("https://example.com/a")
    out = list(sd_mw.process_spider_output(resp_b, [cycle]))
    # a was first discovered at depth 0, so the cycle back at depth 2 does
    # not re-expand the recorded outputs of a
    assert out == [cycle]
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") is None


def test_reexpand_cascades(sd_mw: DepthMiddleware, sd_crawler: Crawler) -> None:
    resp_x = _response("https://example.com/x", depth=1)
    out = list(sd_mw.process_spider_output(resp_x, [Request("https://example.com/y")]))
    assert len(out) == 1  # y at depth 2

    resp_y = _response("https://example.com/y", depth=2)
    out = list(sd_mw.process_spider_output(resp_y, [Request("https://example.com/z")]))
    assert out == []  # z at depth 3 > DEPTH_LIMIT 2

    resp_a = _response("https://example.com/a", depth=5)
    rediscovery = Request("https://example.com/x", meta={"depth_reset": True})
    out = list(sd_mw.process_spider_output(resp_a, [rediscovery]))
    assert [r.url for r in out] == [
        "https://example.com/x",
        "https://example.com/y",
        "https://example.com/z",
    ]
    assert [r.meta["depth"] for r in out] == [0, 1, 2]
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") == 2


def test_reexpand_depth_reset_child(
    sd_mw: DepthMiddleware, sd_crawler: Crawler
) -> None:
    resp_x = _response("https://example.com/x", depth=2)
    children = [
        Request("https://example.com/y"),
        Request("https://example.com/z", meta={"depth_reset": True}),
    ]
    out = list(sd_mw.process_spider_output(resp_x, children))
    # y is dropped (depth 3 > DEPTH_LIMIT 2), z gets depth 0
    assert [r.url for r in out] == ["https://example.com/z"]

    resp_a = _response("https://example.com/a", depth=0)
    rediscovery = Request("https://example.com/x")
    out = list(sd_mw.process_spider_output(resp_a, [rediscovery]))
    # y is re-expanded at depth 2, while z already has the lowest possible
    # depth and is not emitted again
    assert [r.url for r in out] == [
        "https://example.com/x",
        "https://example.com/y",
    ]
    assert out[1].meta["depth"] == 2
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") == 1


def test_response_depth_uses_shortest_discovery(sd_mw: DepthMiddleware) -> None:
    # x is discovered at depth 1 while its depth-2 request is already
    # scheduled, so its response outputs get depths relative to depth 1
    resp_a = _response("https://example.com/a", depth=0)
    out = list(sd_mw.process_spider_output(resp_a, [Request("https://example.com/x")]))
    assert out[0].meta["depth"] == 1

    resp_x = _response("https://example.com/x", depth=2)
    out = list(sd_mw.process_spider_output(resp_x, [Request("https://example.com/y")]))
    assert len(out) == 1
    assert out[0].meta["depth"] == 2
    assert resp_x.meta["depth"] == 1


def test_record_output_uses_latest_shortest_depth() -> None:
    crawler = get_crawler(Spider, {"DEPTH_LIMIT": 5, "DEPTH_SHORTEST_DISCOVERY": True})
    crawler.stats.open_spider()
    mw = build_from_crawler(DepthMiddleware, crawler)
    resp_x = _response("https://example.com/x", depth=3)
    out = iter(
        mw.process_spider_output(
            resp_x,
            [Request("https://example.com/y"), Request("https://example.com/z")],
        )
    )
    assert next(out).meta["depth"] == 4  # y at depth 4

    # x is discovered at depth 1 while its response is still being processed
    resp_a = _response("https://example.com/a", depth=0)
    list(mw.process_spider_output(resp_a, [Request("https://example.com/x")]))

    # the remaining outputs of x get depths relative to the shorter path
    assert next(out).meta["depth"] == 2  # z at depth 2
    assert list(out) == []
    assert resp_x.meta["depth"] == 1
    crawler.stats.close_spider()


def test_reexpand_still_over_limit(sd_mw: DepthMiddleware, sd_crawler: Crawler) -> None:
    resp_x = _response("https://example.com/x", depth=3)
    out = list(sd_mw.process_spider_output(resp_x, [Request("https://example.com/y")]))
    assert out == []  # y at depth 4 > DEPTH_LIMIT 2

    resp_a = _response("https://example.com/a", depth=1)
    rediscovery = Request("https://example.com/x")
    out = list(sd_mw.process_spider_output(resp_a, [rediscovery]))
    # y is re-expanded at depth 3, which still exceeds DEPTH_LIMIT 2
    assert out == [rediscovery]
    assert sd_crawler.stats.get_value("depth/request_reexpanded_count") is None


def test_spider_open_close_without_jobdir(sd_mw: DepthMiddleware) -> None:
    spider = Spider(name="test")
    sd_mw.spider_opened(spider)  # no-op without JOBDIR
    sd_mw.spider_closed(spider)


def test_jobdir_persistence(tmp_path: Path) -> None:
    settings = {
        "DEPTH_LIMIT": 2,
        "DEPTH_SHORTEST_DISCOVERY": True,
        "JOBDIR": str(tmp_path),
    }
    crawler = get_crawler(Spider, settings)
    crawler.stats.open_spider()
    spider = Spider.from_crawler(crawler, name="test")
    mw = build_from_crawler(DepthMiddleware, crawler)
    mw.spider_opened(spider)  # no state file yet
    resp_x = _response("https://example.com/x", depth=2)
    out = list(
        mw.process_spider_output(
            resp_x, [Request("https://example.com/y", callback=spider.parse)]
        )
    )
    assert out == []
    mw.spider_closed(spider)
    assert (tmp_path / "requests.depths").exists()
    crawler.stats.close_spider()

    crawler = get_crawler(Spider, settings)
    crawler.stats.open_spider()
    spider = Spider.from_crawler(crawler, name="test")
    mw = build_from_crawler(DepthMiddleware, crawler)
    mw.spider_opened(spider)
    resp_a = _response("https://example.com/a", depth=0)
    out = list(mw.process_spider_output(resp_a, [Request("https://example.com/x")]))
    assert len(out) == 2
    assert out[1].url == "https://example.com/y"
    assert out[1].meta["depth"] == 2
    assert out[1].callback == spider.parse
    mw.spider_closed(spider)
    crawler.stats.close_spider()


def test_jobdir_unserializable_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    crawler = get_crawler(
        Spider,
        {
            "DEPTH_LIMIT": 2,
            "DEPTH_SHORTEST_DISCOVERY": True,
            "JOBDIR": str(tmp_path),
        },
    )
    crawler.stats.open_spider()
    spider = Spider.from_crawler(crawler, name="test")
    mw = build_from_crawler(DepthMiddleware, crawler)
    resp_x = _response("https://example.com/x", depth=2)
    child = Request("https://example.com/y", meta={"unserializable": lambda: None})
    list(mw.process_spider_output(resp_x, [child]))
    with caplog.at_level("WARNING", logger="scrapy.spidermiddlewares.depth"):
        mw.spider_closed(spider)
    assert "Unable to serialize the depth state" in caplog.text
    assert not (tmp_path / "requests.depths").exists()
    crawler.stats.close_spider()


class ShortestDepthSpider(Spider):
    """Crawl a fixed graph of ``data:`` URLs, so that no server is needed.

    start links to both ends of the graph: a long path (a -> b -> x) and a
    short path (c -> x). x links to y and z, which exceed DEPTH_LIMIT 3 when
    x is downloaded at depth 3 through the long path.
    """

    name = "shortest-depth"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.visited: list[str] = []
        self.x_parse_count = 0

    async def start(self) -> AsyncIterator[Request]:
        yield Request("data:,start", callback=self.parse)

    def parse(self, response: Response) -> Iterator[Any]:
        node = response.url.split(",", 1)[1]
        self.visited.append(node)
        if node == "x":
            self.x_parse_count += 1
            yield {"node": node}
        for next_node in self._next_nodes(node):
            yield Request(f"data:,{next_node}", callback=self.parse)

    def _next_nodes(self, node: str) -> list[str]:
        graph: dict[str, list[str]] = {
            "start": ["c", "a"],
            "a": ["b"],
            "b": ["x"],
            "c": ["x"],
            "x": ["y", "z"],
        }
        return graph.get(node, [])


class TestShortestDiscoveryCrawl:
    @pytest.mark.parametrize("order", ["long-first", "short-first"])
    @coroutine_test
    async def test_crawl_result_does_not_depend_on_order(
        self, order: str, tmp_path: Path
    ) -> None:
        gate: Deferred[None] = Deferred()
        gated_node = "c" if order == "long-first" else "a"

        class GateDownloaderMiddleware:
            """Hold back the gated branch until the gate is opened."""

            async def process_request(self, request: Request) -> None:
                if request.url == f"data:,{gated_node}":
                    await maybe_deferred_to_future(gate)

        class GateOpenerSpiderMiddleware:
            """Open the gate once the outputs of x have been processed."""

            # Placed after DepthMiddleware so that, by the time the outputs
            # of x pass through, they have been recorded, and the gated
            # branch can only reach x afterwards.
            async def process_spider_output(
                self, response: Response, result: AsyncIterator[Any]
            ) -> AsyncIterator[Any]:
                async for output in result:
                    yield output
                if response.url == "data:,x" and not gate.called:
                    gate.callback(None)

        settings = {
            "CONCURRENT_REQUESTS": 2,
            "DEPTH_LIMIT": 3,
            "DEPTH_SHORTEST_DISCOVERY": True,
            "DOWNLOADER_MIDDLEWARES": {GateDownloaderMiddleware: 1},
            "SPIDER_MIDDLEWARES": {GateOpenerSpiderMiddleware: 543},
            "JOBDIR": str(tmp_path),
        }
        crawler = get_crawler(ShortestDepthSpider, settings)
        items: list[Any] = []

        def _on_item_scraped(item: Any) -> None:
            items.append(item)

        crawler.signals.connect(_on_item_scraped, signals.item_scraped)
        await crawler.crawl_async()
        spider = crawler.spider
        assert isinstance(spider, ShortestDepthSpider)
        assert sorted(spider.visited) == ["a", "b", "c", "start", "x", "y", "z"]
        assert spider.x_parse_count == 1
        assert items == [{"node": "x"}]
        reexpanded = crawler.stats.get_value("depth/request_reexpanded_count") or 0
        assert reexpanded == (2 if order == "long-first" else 0)
        assert (tmp_path / "requests.depths").exists()
