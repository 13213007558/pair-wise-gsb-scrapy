from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

from scrapy import signals
from scrapy.http import Request, Response, TextResponse
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


class _DepthGraphSpider(MockServerSpider):
    name = "depth_graph"
    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DEPTH_LIMIT": 2,
        "DUPEFILTER_SHORTEST_DEPTH": True,
    }

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._first_x_done = asyncio.Event()
        self._second_x_done = asyncio.Event()
        self._x_callbacks = 0

    async def start(self) -> AsyncIterator[Request]:
        assert self.mockserver
        yield Request(
            self.mockserver.url("/depth-graph/gateway-a"),
            priority=10,
            callback=self.parse,
        )
        await self._first_x_done.wait()
        yield Request(
            self.mockserver.url("/depth-graph/home"),
            priority=-10,
            callback=self.parse,
        )
        await self._second_x_done.wait()
        yield Request(
            self.mockserver.url("/depth-graph/gateway-b"),
            priority=-20,
            callback=self.parse,
        )

    def parse(self, response: Response) -> Any:
        raise NotImplementedError

    def _links(self, response: TextResponse) -> Iterator[Any]:
        yield {"url": response.url}
        for href in response.css("a::attr(href)").getall():
            yield Request(response.urljoin(href), callback=self.parse)
        if urlsplit(response.url).path.endswith("/x"):
            self._x_callbacks += 1
            if self._x_callbacks == 1:
                self._first_x_done.set()
            elif self._x_callbacks == 2:
                self._second_x_done.set()


class _SyncDepthGraphSpider(_DepthGraphSpider):
    def parse(self, response: Response) -> Iterator[Any]:
        assert isinstance(response, TextResponse)
        yield from self._links(response)


class _AsyncDepthGraphSpider(_DepthGraphSpider):
    async def parse(self, response: Response) -> AsyncIterator[Any]:
        assert isinstance(response, TextResponse)
        for output in self._links(response):
            yield output


def _path(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


async def _crawl_depth_graph(
    spider_cls: type[_DepthGraphSpider], mockserver: MockServer
) -> tuple[list[str], Crawler]:
    urls: list[str] = []

    def collect(item: dict[str, str]) -> None:
        urls.append(_path(item["url"]))

    crawler = get_crawler(spider_cls)
    crawler.signals.connect(collect, signals.item_scraped)
    await crawler.crawl_async(mockserver=mockserver)
    return urls, crawler


@pytest.mark.parametrize(
    "spider_cls",
    [_SyncDepthGraphSpider, _AsyncDepthGraphSpider],
)
@coroutine_test
async def test_shortest_depth_graph(
    spider_cls: type[_DepthGraphSpider], mockserver: MockServer
) -> None:
    urls, crawler = await _crawl_depth_graph(spider_cls, mockserver)

    assert urls == [
        "gateway-a",
        "a",
        "x",
        "home",
        "x",
        "ring",
        "y",
        "gateway-b",
        "b",
    ]
    assert crawler.stats.get_value("dupefilter/filtered") == 2
    assert crawler.stats.get_value("depth/request_ignored_count") == 4
    assert crawler.stats.get_value("downloader/response_count") == 9
