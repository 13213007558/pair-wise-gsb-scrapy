from __future__ import annotations

import gzip
import re
from datetime import date, datetime, timezone
from io import BytesIO
from logging import WARNING
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import pytest

from scrapy import signals
from scrapy.exceptions import DropItem
from scrapy.http import HtmlResponse, Request, Response, TextResponse, XmlResponse
from scrapy.spiders import SitemapSpider
from scrapy.spiders.sitemap import _Lastmod, _parse_lastmod, _select_lastmod
from scrapy.utils.test import get_crawler
from tests import tests_datadir
from tests.spiders import RawResponseSpider
from tests.utils.bases.spider import TestSpiderBase
from tests.utils.crawl import crawl_items
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


class RawSitemapSpider(RawResponseSpider):
    """Feeds :meth:`raw_body` to :class:`~scrapy.spiders.SitemapSpider` as a
    sitemap, so that it is fetched and followed through a regular crawl.

    Subclasses build the document in :meth:`raw_body`, typically using
    :attr:`mockserver` to point ``<loc>`` entries at real endpoints.
    """

    content_type = "application/xml"

    async def start(self):
        self.sitemap_urls = [self.raw_url]
        async for request in super().start():
            yield request


def _sitemap_body(
    mockserver: MockServer,
    entries: tuple[tuple[str, str, list[str] | None], ...],
) -> bytes:
    url_tags = "\n".join(
        f'<url><loc>{mockserver.url(path)}</loc>'
        f'{f"<lastmod>{lastmod}</lastmod>" if lastmod else ""}'
        + "".join(
            f'<xhtml:link rel="alternate" href="{mockserver.url(alt)}"/> '
            for alt in alternates
        )
        + "</url>"
        for path, lastmod, alternates in entries
    )
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        b'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        + url_tags.encode()
        + b"</urlset>"
    )


class _SitemapStateSpider(RawSitemapSpider, SitemapSpider):
    name = "sitemap_state"
    sitemap_alternate_links = True

    def __init__(
        self,
        *args,
        sitemap_entries: tuple[
            tuple[tuple[str, str, list[str] | None], ...], ...
        ],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._sitemap_entries = sitemap_entries

    def parse(self, response):
        yield {"url": response.url}

    async def start(self):
        self.sitemap_urls = [self.raw_url]
        async for request in super().start():
            yield request

    def raw_body(self):
        assert self.mockserver
        urls = [self._raw_url(entries) for entries in self._sitemap_entries]
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
            + "</sitemapindex>"
        )

    def _raw_url(self, entries: tuple[tuple[str, str, list[str] | None], ...]) -> str:
        assert self.mockserver
        raw = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/xml\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{_sitemap_body(self.mockserver, entries).decode()}"
        )
        return self.mockserver.url("/raw?" + urlencode({"raw": raw}))


class _DropSitemapItemPipeline:
    async def process_item(self, item):
        if item["url"].endswith("/drop"):
            raise DropItem("dropped")
        return item


class _FailingSitemapItemPipeline(_DropSitemapItemPipeline):
    async def process_item(self, item):
        if item["url"].endswith("/error"):
            raise RuntimeError("pipeline error")
        return await super().process_item(item)


class TestSitemapSpider(TestSpiderBase):
    spider_class = SitemapSpider

    BODY = b"SITEMAP"
    f = BytesIO()
    g = gzip.GzipFile(fileobj=f, mode="w+b")
    g.write(BODY)
    g.close()
    GZBODY = f.getvalue()

    @pytest.mark.parametrize(
        ("raw", "precision", "expected"),
        [
            ("2024-01-01", "date", "2024-01-01"),
            (
                "2024-01-01T12:00:00+02:00",
                "datetime",
                "2024-01-01T10:00:00+00:00",
            ),
            ("2024-01-01T10:00:00Z", "datetime", "2024-01-01T10:00:00+00:00"),
        ],
    )
    def test_parse_sitemap_lastmod(self, raw: str, precision: str, expected: str):
        parsed_expected: date | datetime = (
            date.fromisoformat(expected)
            if precision == "date"
            else datetime.fromisoformat(expected)
        )
        assert _parse_lastmod(raw) == _Lastmod(precision, parsed_expected)

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "2024-01-01T10:00:00",
            "2024-01-01T10:00:00+00",
            "2024-02-30",
            "not a date",
        ],
    )
    def test_parse_invalid_sitemap_lastmod(self, raw: str):
        assert _parse_lastmod(raw) is None

    def test_select_sitemap_lastmod_requires_matching_precision(self):
        date_version = _parse_lastmod("2024-01-01")
        datetime_version = _parse_lastmod("2024-01-01T00:00:00+00:00")
        assert date_version is not None
        assert datetime_version is not None
        assert _select_lastmod([date_version, datetime_version]) is None

    def test_sitemapindex_lastmod_is_not_page_version(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        index = TextResponse(
            "http://example.com/sitemap.xml",
            request=Request(
                "http://example.com/sitemap.xml",
                callback=spider._parse_sitemap,
                meta={"_sitemap": True},
            ),
            body=(
                b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<sitemap><loc>http://example.com/pages.xml</loc>'
                b'<lastmod>2024-01-01</lastmod></sitemap></sitemapindex>'
            ),
        )

        request = next(iter(spider._parse_sitemap(index)))

        assert request.url == "http://example.com/pages.xml"
        assert "_sitemap_lastmod" not in request.meta

    def test_sitemap_error_finishes_incremental_sitemap(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        request = Request(
            "http://example.com/sitemap.xml",
            meta={"_sitemap": True},
        )
        spider._pending_sitemaps = 1

        spider._sitemap_error(type("_Failure", (), {"request": request})())

        assert spider._pending_sitemaps == 0

    def test_incremental_sitemap_merges_duplicate_lastmods(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {}
        scheduled: list[Request] = []
        spider.crawler.engine = type(
            "_Engine",
            (),
            {"crawl": lambda self, request: scheduled.append(request)},
        )()

        first = TextResponse(
            "http://example.com/sitemap1.xml",
            request=Request(
                "http://example.com/sitemap1.xml",
                meta={"_sitemap": True, "_sitemap_parent": True},
            ),
            body=(
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<url><loc>http://example.com/page</loc>'
                b'<lastmod>2024-01-02</lastmod></url></urlset>'
            ),
        )
        second = TextResponse(
            "http://example.com/sitemap2.xml",
            request=Request(
                "http://example.com/sitemap2.xml",
                meta={"_sitemap": True, "_sitemap_parent": True},
            ),
            body=(
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<url><loc>http://example.com/page</loc>'
                b'<lastmod>2024-01-01</lastmod></url></urlset>'
            ),
        )

        spider._pending_sitemaps = 2
        assert list(spider._parse_sitemap(first)) == []
        spider._pending_sitemaps = 1
        assert list(spider._parse_sitemap(second)) == []

        assert len(scheduled) == 1
        assert scheduled[0].url == "http://example.com/page"
        assert scheduled[0].meta["_sitemap_lastmod"] == ("date", "2024-01-02")

    def test_incremental_sitemap_refresh_ignores_stored_lastmod(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True, sitemap_refresh=True
        )
        spider.state = {
            "_sitemap_lastmods": {
                "http://example.com/page": ("date", "2024-01-03")
            }
        }
        scheduled: list[Request] = []
        spider.crawler.engine = type(
            "_Engine",
            (),
            {"crawl": lambda self, request: scheduled.append(request)},
        )()
        sitemap = TextResponse(
            "http://example.com/sitemap.xml",
            request=Request(
                "http://example.com/sitemap.xml",
                meta={"_sitemap": True},
            ),
            body=(
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<url><loc>http://example.com/page</loc>'
                b'<lastmod>2024-01-02</lastmod></url></urlset>'
            ),
        )
        spider._pending_sitemaps = 1

        assert list(spider._parse_sitemap(sitemap)) == []

        assert len(scheduled) == 1
        assert scheduled[0].url == "http://example.com/page"

    def test_incremental_sitemap_skips_current_lastmod(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {
            "_sitemap_lastmods": {
                "http://example.com/page": ("date", "2024-01-03")
            }
        }
        scheduled: list[Request] = []
        spider.crawler.engine = type(
            "_Engine",
            (),
            {"crawl": lambda self, request: scheduled.append(request)},
        )()
        sitemap = TextResponse(
            "http://example.com/sitemap.xml",
            request=Request(
                "http://example.com/sitemap.xml",
                meta={"_sitemap": True},
            ),
            body=(
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<url><loc>http://example.com/page</loc>'
                b'<lastmod>2024-01-02</lastmod></url></urlset>'
            ),
        )
        spider._pending_sitemaps = 1

        assert list(spider._parse_sitemap(sitemap)) == []

        assert scheduled == []

    def test_incremental_sitemap_ignores_invalid_stored_state(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {
            "_sitemap_lastmods": {
                "http://example.com/bad": ("time", "2024-01-01"),
                "http://example.com/page": ("date", "2024-01-02"),
            }
        }

        assert spider._lastmods == {
            "http://example.com/page": ("date", "2024-01-02")
        }
        assert spider.state["_sitemap_lastmods"] == {
            "http://example.com/page": ("date", "2024-01-02")
        }

    @coroutine_test
    @pytest.mark.parametrize("signal_name", ["item_scraped", "item_dropped"])
    async def test_incremental_state_waits_for_item_pipeline(
        self, signal_name: str
    ):
        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                yield {"url": response.url}

        crawler = get_crawler()
        spider = _Spider.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {}
        response = TextResponse(
            "http://example.com/page",
            request=Request(
                "http://example.com/page",
                meta={"_sitemap_lastmod": ("date", "2024-01-02")},
            ),
            body=b"",
        )

        async def collect() -> list[dict[str, str]]:
            return [
                output
                async for output in spider._parse_sitemap_page(response, "parse")
            ]

        outputs = await collect()
        assert outputs == [{"url": "http://example.com/page"}]
        assert spider.state.get("_sitemap_lastmods", {}) == {}
        signal_kwargs = {
            "item": outputs[0],
            "response": response,
            "spider": spider,
        }
        if signal_name == "item_dropped":
            signal_kwargs["exception"] = DropItem("dropped")
        crawler.signals.send_catch_log(
            getattr(signals, signal_name), **signal_kwargs
        )
        assert spider.state["_sitemap_lastmods"]["http://example.com/page"] == (
            "date",
            "2024-01-02",
        )

    @coroutine_test
    async def test_incremental_state_ignores_pipeline_error(self):
        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                yield {"url": response.url}

        crawler = get_crawler()
        spider = _Spider.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {}
        response = TextResponse(
            "http://example.com/page",
            request=Request(
                "http://example.com/page",
                meta={"_sitemap_lastmod": ("date", "2024-01-02")},
            ),
            body=b"",
        )

        async for _ in spider._parse_sitemap_page(response, "parse"):
            pass
        crawler.signals.send_catch_log(
            signals.item_error,
            item={"url": response.url},
            response=response,
            spider=spider,
            failure=None,
        )

        assert spider.state.get("_sitemap_lastmods", {}) == {}

    @coroutine_test
    async def test_incremental_state_records_callback_without_items(self):
        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                return None

        crawler = get_crawler()
        spider = _Spider.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {}
        response = TextResponse(
            "http://example.com/page",
            request=Request(
                "http://example.com/page",
                meta={"_sitemap_lastmod": ("date", "2024-01-02")},
            ),
            body=b"",
        )

        outputs = [
            output
            async for output in spider._parse_sitemap_page(response, "parse")
        ]

        assert outputs == []
        assert spider.state["_sitemap_lastmods"]["http://example.com/page"] == (
            "date",
            "2024-01-02",
        )

    @coroutine_test
    async def test_incremental_state_ignores_callback_error(self):
        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                raise RuntimeError("callback error")

        crawler = get_crawler()
        spider = _Spider.from_crawler(
            crawler, "example.com", sitemap_incremental=True
        )
        spider.state = {}
        response = TextResponse(
            "http://example.com/page",
            request=Request(
                "http://example.com/page",
                meta={"_sitemap_lastmod": ("date", "2024-01-02")},
            ),
            body=b"",
        )

        with pytest.raises(RuntimeError, match="callback error"):
            async for _ in spider._parse_sitemap_page(response, "parse"):
                pass

        assert spider.state.get("_sitemap_lastmods", {}) == {}

    def assertSitemapBody(self, response: Response, body: bytes | None) -> None:
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        assert spider._get_sitemap_body(response) == body

    def test_get_sitemap_body(self):
        r: Response = XmlResponse(url="http://www.example.com/", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

        r = HtmlResponse(url="http://www.example.com/", body=self.BODY)
        self.assertSitemapBody(r, None)

        r = Response(url="http://www.example.com/favicon.ico", body=self.BODY)
        self.assertSitemapBody(r, None)

        r = XmlResponse(url="http://www.example.com/", body=b"")
        self.assertSitemapBody(r, b"")

    def test_get_sitemap_body_gzip_headers(self):
        r = Response(
            url="http://www.example.com/sitemap",
            body=self.GZBODY,
            headers={"content-type": "application/gzip"},
            request=Request("http://www.example.com/sitemap"),
        )
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_body_xml_url(self):
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_body_xml_url_compressed(self):
        r = Response(
            url="http://www.example.com/sitemap.xml.gz",
            body=self.GZBODY,
            request=Request("http://www.example.com/sitemap"),
        )
        self.assertSitemapBody(r, self.BODY)

        # .xml.gz but body decoded by HttpCompression middleware already
        r = Response(url="http://www.example.com/sitemap.xml.gz", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_urls_from_robotstxt(self):
        robots = b"""# Sitemap files
Sitemap: http://example.com/sitemap.xml
Sitemap: http://example.com/sitemap-product-index.xml
Sitemap: HTTP://example.com/sitemap-uppercase.xml
Sitemap: /sitemap-relative-url.xml
"""

        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://example.com/sitemap.xml",
            "http://example.com/sitemap-product-index.xml",
            "http://example.com/sitemap-uppercase.xml",
            "http://www.example.com/sitemap-relative-url.xml",
        ]

    def test_get_sitemap_urls_from_robotstxt_skips_invalid_utf8_urls(self):
        robots = (
            b"User-agent: *\n"
            b"Sitemap: http://example.com/\xff.xml\n"
            b"Sitemap: http://example.com/ok.xml\n"
        )

        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        spider = self.spider_class("example.com")

        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://example.com/ok.xml",
        ]

    def test_alternate_url_locs(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/deutsch/"/>
            <xhtml:link rel="alternate" hreflang="de-ch"
                href="http://www.example.com/schweiz-deutsch/"/>
            <xhtml:link rel="alternate" hreflang="it"
                href="http://www.example.com/italiano/"/>
            <xhtml:link rel="alternate" hreflang="it"/><!-- wrong tag without href -->
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/"
        ]

        spider.sitemap_alternate_links = True
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/",
            "http://www.example.com/deutsch/",
            "http://www.example.com/schweiz-deutsch/",
            "http://www.example.com/italiano/",
        ]

    def test_sitemap_filter(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/</loc>
            <lastmod>2010-01-01</lastmod>
        </url>
        <url>
            <loc>http://www.example.com/portuguese/</loc>
            <lastmod>2005-01-01</lastmod>
        </url>
    </urlset>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    date_time = datetime.strptime(entry["lastmod"], "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if date_time.year > 2008:
                        yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/",
            "http://www.example.com/portuguese/",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/"
        ]

    def test_sitemap_filter_with_alternate_links(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/article_1/</loc>
            <lastmod>2010-01-01</lastmod>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/deutsch/article_1/"/>
        </url>
        <url>
            <loc>http://www.example.com/english/article_2/</loc>
            <lastmod>2015-01-01</lastmod>
        </url>
    </urlset>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    alternate_links = entry.get("alternate", ())
                    for link in alternate_links:
                        if "/deutsch/" in link:
                            entry["loc"] = link
                            yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/article_1/",
            "http://www.example.com/english/article_2/",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/deutsch/article_1/"
        ]

    def test_sitemapindex_filter(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>http://www.example.com/sitemap1.xml</loc>
            <lastmod>2004-01-01T20:00:00+00:00</lastmod>
        </sitemap>
        <sitemap>
            <loc>http://www.example.com/sitemap2.xml</loc>
            <lastmod>2005-01-01</lastmod>
        </sitemap>
    </sitemapindex>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    date_time = datetime.strptime(
                        entry["lastmod"].split("T")[0], "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                    if date_time.year > 2004:
                        yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/sitemap1.xml",
            "http://www.example.com/sitemap2.xml",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/sitemap2.xml"
        ]

    @pytest.mark.parametrize(
        ("rule", "result"),
        [(r"english", ["http://www.example.com/english/"]), (r"nonexistent", [])],
    )
    def test_sitemap_filter_with_rule(self, rule: str, result: list[str]):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>http://www.example.com/english/</loc></url>
        <url><loc>http://www.example.com/portuguese/</loc></url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _RuleSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_rules = [(rule, "parse")]

        spider = _RuleSpider("example.com")
        urls = [req.url for req in spider._parse_sitemap(r)]
        assert urls == result

    @coroutine_test
    async def test_sitemap_rules_with_callable(self, mockserver: MockServer):
        # A sitemap_rules entry may hold a callable instead of a method name.
        def parse_item(response):
            yield {"url": response.url}

        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_rules = [("", parse_item)]

            def raw_body(self):
                assert self.mockserver
                loc = self.mockserver.url("/text")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{loc}</loc></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text")}]

    @coroutine_test
    async def test_sitemap_incremental_lastmods(
        self, mockserver: MockServer, tmp_path
    ):
        first_sitemaps = (
            (
                ("/page-unchanged", "2024-01-01", ["/page-alt"]),
                ("/page-changed", "2024-01-01", None),
                ("/page-absolute", "2024-01-01T10:00:00+00:00", None),
                ("/page-mixed", "2024-01-01", None),
                ("/page-invalid", "2024-01-01", None),
                ("/page-duplicate", "2024-01-03", None),
            ),
            (
                ("/page-duplicate", "2024-01-02", None),
                ("/page-drop", "", None),
                ("/page-error", "2024-01-01", None),
            ),
        )
        second_sitemaps = (
            (
                ("/page-unchanged", "2024-01-01", ["/page-alt"]),
                ("/page-changed", "2024-01-02", None),
                ("/page-absolute", "2024-01-01T12:00:00+02:00", None),
                ("/page-mixed", "2024-01-02T00:00:00+00:00", None),
                ("/page-invalid", "not a date", None),
                ("/page-duplicate", "2024-01-03", None),
            ),
            (
                ("/page-duplicate", "2024-01-02", None),
                ("/page-drop", "", None),
                ("/page-error", "2024-01-01", None),
            ),
        )

        async def crawl(
            entries: tuple[
                tuple[tuple[str, str, list[str] | None], ...], ...
            ],
            *,
            refresh: bool = False,
        ) -> tuple[list[str], Crawler]:
            requested_pages: list[str] = []

            def track_request(request: Request) -> None:
                path = urlsplit(request.url).path
                if path.startswith("/page-"):
                    requested_pages.append(path)

            crawler = get_crawler(
                _SitemapStateSpider,
                {
                    "JOBDIR": str(tmp_path),
                    "ITEM_PIPELINES": {_FailingSitemapItemPipeline: 1},
                },
            )
            crawler.signals.connect(
                track_request, signal=signals.request_reached_downloader
            )
            await crawler.crawl_async(
                mockserver=mockserver,
                sitemap_entries=entries,
                sitemap_incremental="1",
                sitemap_refresh=refresh,
            )
            return requested_pages, crawler

        first_pages, _ = await crawl(first_sitemaps)
        second_pages, crawler = await crawl(second_sitemaps)
        refresh_pages, _ = await crawl(second_sitemaps, refresh=True)

        assert set(first_pages) == {
            "/page-alt",
            "/page-absolute",
            "/page-changed",
            "/page-drop",
            "/page-duplicate",
            "/page-error",
            "/page-invalid",
            "/page-mixed",
            "/page-unchanged",
        }
        assert set(second_pages) == {
            "/page-changed",
            "/page-drop",
            "/page-error",
            "/page-invalid",
            "/page-mixed",
        }
        assert set(refresh_pages) == set(first_pages)
        assert "/page-error" not in crawler.spider.state["_sitemap_lastmods"]

    @coroutine_test
    async def test_sitemap_empty_loc(self, mockserver: MockServer):
        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                assert self.mockserver
                loc = self.mockserver.url("/text")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc></loc></url>"
                    f"<url><loc>{loc}</loc></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text")}]

    def test_parse_sitemap_empty_body(self, caplog: pytest.LogCaptureFixture) -> None:
        r = XmlResponse(url="http://www.example.com/sitemap.xml", body=b"")
        spider = self.spider_class("example.com")

        caplog.clear()
        with caplog.at_level(WARNING):
            results = list(spider._parse_sitemap(r))

        assert not results

        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                "Ignoring invalid sitemap: <200 http://www.example.com/sitemap.xml>",
            )
        ]

    def test_parse_sitemap_not_sitemap(self):
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <some attr="string">
        <tag><tag3>sometext</tag3></tag>
        <tag2><tag4>sometext2</tag4></tag2>
    </some>"""
        r = XmlResponse(url="http://www.example.com/random.xml", body=body)
        spider = self.spider_class("example.com")

        results = list(spider._parse_sitemap(r))

        assert not results

    @pytest.mark.parametrize(
        ("follow", "result"),
        [
            (r"1.xml", ["http://www.example.com/sitemap1.xml"]),
            (re.compile(r"sitemap\d"), ["http://www.example.com/sitemap1.xml"]),
            (r"nonexistent", []),
        ],
    )
    def test_sitemap_follow(self, follow, result):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>http://www.example.com/sitemap1.xml</loc>
        </sitemap>
    </sitemapindex>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _FollowSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_follow = [follow]

        spider = _FollowSpider("example.com")
        urls = [req.url for req in spider._parse_sitemap(r)]
        assert urls == result

    def test_compression_bomb_setting(self):
        settings = {"DOWNLOAD_MAXSIZE": 10_000_000}
        crawler = get_crawler(settings_dict=settings)
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    def test_compression_bomb_spider_attr(self):
        class DownloadMaxSizeSpider(self.spider_class):  # type: ignore[name-defined,misc]
            download_maxsize = 10_000_000

        crawler = get_crawler()
        spider = DownloadMaxSizeSpider.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    def test_compression_bomb_request_meta(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_maxsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    def test_download_warnsize_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = {"DOWNLOAD_WARNSIZE": 10_000_000}
        crawler = get_crawler(settings_dict=settings)
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    def test_download_warnsize_spider_attr(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class DownloadWarnSizeSpider(self.spider_class):  # type: ignore[name-defined,misc]
            download_warnsize = 10_000_000

        crawler = get_crawler()
        spider = DownloadWarnSizeSpider.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_warnsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    def test_download_warnsize_request_meta(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_warnsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    @coroutine_test
    async def test_sitemap_urls(self):
        class TestSpider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "test"
            sitemap_urls = ["https://toscrape.com/sitemap.xml"]

        crawler = get_crawler(TestSpider)
        spider = TestSpider.from_crawler(crawler)
        requests = [request async for request in spider.start()]

        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://toscrape.com/sitemap.xml"
        assert request.dont_filter is False
        assert request.callback == spider._parse_sitemap
