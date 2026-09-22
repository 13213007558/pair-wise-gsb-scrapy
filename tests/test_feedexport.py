from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import marshal
import os
import pickle
import subprocess
import sys
import tempfile
from logging import getLogger
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from unittest import mock

import lxml.etree
import pytest
from w3lib.url import file_uri_to_path

import scrapy
from scrapy import Spider, signals
from scrapy.exceptions import NotConfigured
from scrapy.exporters import CsvItemExporter, JsonItemExporter
from scrapy.extensions.feedexport import (
    BlockingFeedStorage,
    FeedExporter,
    FeedSlot,
    FileFeedStorage,
    ItemFilter,
    _FeedManifest,
    apply_uri_params,
)
from scrapy.extensions.postprocessing import GzipPlugin
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.python import to_unicode
from scrapy.utils.test import get_crawler
from tests.spiders import ItemSpider
from tests.utils.bases.feedexport import TestFeedExportBase
from tests.utils.decorators import coroutine_test, inline_callbacks_test
from tests.utils.feedexport import MyItem, MyItem2, path_to_url, printf_escape

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable


class FromCrawlerMixin:
    init_with_crawler = False

    @classmethod
    def from_crawler(cls, crawler, *args, feed_options=None, **kwargs):
        cls.init_with_crawler = True
        return cls(*args, **kwargs)


class FromCrawlerCsvItemExporter(CsvItemExporter, FromCrawlerMixin):
    pass


class FromCrawlerFileFeedStorage(FileFeedStorage, FromCrawlerMixin):
    @classmethod
    def from_crawler(cls, crawler, *args, feed_options=None, **kwargs):
        cls.init_with_crawler = True
        return cls(*args, feed_options=feed_options, **kwargs)


class DummyBlockingFeedStorage(BlockingFeedStorage):
    def __init__(self, uri, *args, feed_options=None):
        self.path = Path(file_uri_to_path(uri))

    def _store_in_thread(self, file):
        dirname = self.path.parent
        if dirname and not dirname.exists():
            dirname.mkdir(parents=True)
        with self.path.open("ab") as output_file:
            output_file.write(file.read())

        file.close()


class FailingBlockingFeedStorage(DummyBlockingFeedStorage):
    def _store_in_thread(self, file):
        file.close()
        raise OSError("Cannot store")


class LogOnStoreFileStorage:
    """
    This storage logs inside `store` method.
    It can be used to make sure `store` method is invoked.
    """

    def __init__(self, uri, feed_options=None):
        self.path = file_uri_to_path(uri)
        self.logger = getLogger()

    def open(self, spider):
        return tempfile.NamedTemporaryFile(prefix="feed-")

    def store(self, file):
        self.logger.info("Storage.store is called")
        file.close()


class FailingBatchFileStorage(FileFeedStorage):
    """File storage that fails to store the batch 2 file."""

    def store(self, file):
        file.close()
        if self.path.endswith("batch-2.jl"):
            raise OSError("Cannot store")


class InstrumentedFeedSlot(FeedSlot):
    """Instrumented FeedSlot subclass for keeping track of calls to
    start_exporting and finish_exporting."""

    update_listener: Callable[[str], None]

    def start_exporting(self):
        self.update_listener("start")
        super().start_exporting()

    def finish_exporting(self):
        self.update_listener("finish")
        super().finish_exporting()

    @classmethod
    def subscribe__listener(cls, listener: IsExportingListener) -> None:
        cls.update_listener = listener.update


class IsExportingListener:
    """When subscribed to InstrumentedFeedSlot, keeps track of when
    a call to start_exporting has been made without a closing call to
    finish_exporting and when a call to finish_exporting has been made
    before a call to start_exporting."""

    def __init__(self) -> None:
        self.start_without_finish = False
        self.finish_without_start = False

    def update(self, method):
        if method == "start":
            self.start_without_finish = True
        elif method == "finish":
            if self.start_without_finish:
                self.start_without_finish = False
            else:
                self.finish_without_start = True


class ExceptionJsonItemExporter(JsonItemExporter):
    """JsonItemExporter that throws an exception every time export_item is called."""

    def export_item(self, _):
        raise RuntimeError("foo")


class TestFeedExport(TestFeedExportBase):
    async def run_and_export(
        self, spider_cls: type[Spider], settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Run spider with specified settings; return exported data."""

        FEEDS = settings.get("FEEDS") or {}
        settings["FEEDS"] = {
            printf_escape(path_to_url(file_path)): feed_options
            for file_path, feed_options in FEEDS.items()
        }

        content: dict[str, Any] = {}
        try:
            spider_cls.start_urls = [self.mockserver.url("/")]
            crawler = get_crawler(spider_cls, settings)
            await crawler.crawl_async()

            for file_path, feed_options in FEEDS.items():
                content[feed_options["format"]] = (
                    Path(file_path).read_bytes() if Path(file_path).exists() else None
                )

        finally:
            for file_path in FEEDS:
                if not Path(file_path).exists():
                    continue

                Path(file_path).unlink()

        return content

    async def assertExportedCsv(
        self,
        items: Iterable[Any],
        header: Iterable[str],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "csv"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        reader = csv.DictReader(to_unicode(data["csv"]).splitlines())
        assert reader.fieldnames == list(header)
        assert rows == list(reader)

    async def assertExportedJsonLines(
        self,
        items: Iterable[Any],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "jl"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        parsed = [json.loads(to_unicode(line)) for line in data["jl"].splitlines()]
        rows = [{k: v for k, v in row.items() if v} for row in rows]
        assert rows == parsed

    async def assertExportedXml(
        self,
        items: Iterable[Any],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "xml"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        rows = [{k: v for k, v in row.items() if v} for row in rows]
        root = lxml.etree.fromstring(data["xml"])
        got_rows = [{e.tag: e.text for e in it} for it in root.findall("item")]
        assert rows == got_rows

    async def assertExportedMultiple(
        self,
        items: Iterable[Any],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "xml"},
                    self._random_temp_filename(): {"format": "json"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        rows = [{k: v for k, v in row.items() if v} for row in rows]
        # XML
        root = lxml.etree.fromstring(data["xml"])
        xml_rows = [{e.tag: e.text for e in it} for it in root.findall("item")]
        assert rows == xml_rows
        # JSON
        json_rows = json.loads(to_unicode(data["json"]))
        assert rows == json_rows

    async def assertExportedPickle(
        self,
        items: Iterable[Any],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "pickle"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        expected = [{k: v for k, v in row.items() if v} for row in rows]

        result = self._load_until_eof(data["pickle"], load_func=pickle.load)
        assert result == expected

    async def assertExportedMarshal(
        self,
        items: Iterable[Any],
        rows: Iterable[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> None:
        settings = settings or {}
        settings.update(
            {
                "FEEDS": {
                    self._random_temp_filename(): {"format": "marshal"},
                },
            }
        )
        data = await self.exported_data(items, settings)
        expected = [{k: v for k, v in row.items() if v} for row in rows]

        result = self._load_until_eof(data["marshal"], load_func=marshal.load)
        assert result == expected

    @inline_callbacks_test
    def test_stats_file_success(self):
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(self._random_temp_filename()))): {
                    "format": "json",
                }
            },
        }
        crawler = get_crawler(ItemSpider, settings)
        yield crawler.crawl(mockserver=self.mockserver)
        assert "feedexport/success_count/FileFeedStorage" in crawler.stats.get_stats()
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 1

    @inline_callbacks_test
    def test_stats_file_failed(self):
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(self._random_temp_filename()))): {
                    "format": "json",
                }
            },
        }
        crawler = get_crawler(ItemSpider, settings)

        def store(file: IO[bytes]) -> None:
            file.close()
            raise KeyError("foo")

        with mock.patch(
            "scrapy.extensions.feedexport.FileFeedStorage.store",
            side_effect=store,
        ):
            yield crawler.crawl(mockserver=self.mockserver)
        assert "feedexport/failed_count/FileFeedStorage" in crawler.stats.get_stats()
        assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1

    @inline_callbacks_test
    def test_stats_multiple_file(self):
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(self._random_temp_filename()))): {
                    "format": "json",
                },
                "stdout:": {
                    "format": "xml",
                },
            },
        }
        crawler = get_crawler(ItemSpider, settings)
        yield crawler.crawl(mockserver=self.mockserver)
        assert "feedexport/success_count/FileFeedStorage" in crawler.stats.get_stats()
        assert "feedexport/success_count/StdoutFeedStorage" in crawler.stats.get_stats()
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 1
        assert (
            crawler.stats.get_value("feedexport/success_count/StdoutFeedStorage") == 1
        )

    @coroutine_test
    async def test_export_items(self):
        # feed exporters use field names from Item
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
            MyItem({"foo": "bar2", "egg": "spam2", "baz": "quux2"}),
        ]
        rows = [
            {"egg": "spam1", "foo": "bar1", "baz": ""},
            {"egg": "spam2", "foo": "bar2", "baz": "quux2"},
        ]
        header = MyItem.fields.keys()
        await self.assertExported(items, header, rows)

    @coroutine_test
    async def test_pathlib_uri_with_placeholders(self):
        feed_dir = Path(self.temp_dir, "pathlib_placeholders")
        feed_dir.mkdir()
        items = [MyItem({"foo": "bar1", "egg": "spam1"})]

        class TestSpider(scrapy.Spider):
            name = "testspider"

            def parse(self, response):
                yield from items

        TestSpider.start_urls = [self.mockserver.url("/")]
        settings = {
            "FEEDS": {
                feed_dir / "%(time)s.json": {"format": "json"},
            },
        }
        crawler = get_crawler(TestSpider, settings)
        await crawler.crawl_async()

        files = list(feed_dir.iterdir())
        assert len(files) == 1
        assert "%(time)s" not in files[0].name
        assert files[0].suffix == ".json"

    @coroutine_test
    async def test_pathlib_uri_with_spaces_and_unicode(self):
        # A pathlib.Path key with spaces and non-ASCII characters must be kept
        # verbatim (not percent-encoded), while %()s placeholders are still
        # substituted. %(name)s resolves to the spider name deterministically,
        # so the resulting file name can be asserted exactly.
        feed_dir = Path(self.temp_dir, "pathlib_spaces_unicode")
        feed_dir.mkdir()
        items = [MyItem({"foo": "bar1", "egg": "spam1"})]

        class TestSpider(scrapy.Spider):
            name = "testspider"

            def parse(self, response):
                yield from items

        TestSpider.start_urls = [self.mockserver.url("/")]
        settings = {
            "FEEDS": {
                feed_dir / "out %(name)s ünïcode.json": {"format": "json"},
            },
        }
        crawler = get_crawler(TestSpider, settings)
        await crawler.crawl_async()

        files = list(feed_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == "out testspider ünïcode.json"

    @coroutine_test
    async def test_str_uri_with_percent_encoding_and_placeholder(self):
        # A percent-encoded string URI (e.g. %20 for a space) must reach
        # storage verbatim rather than being misinterpreted as a printf
        # directive, while %()s placeholders are still substituted. See #6425
        # and #5794.
        feed_dir = Path(self.temp_dir, "dir with spaces")
        feed_dir.mkdir()
        items = [MyItem({"foo": "bar1", "egg": "spam1"})]

        class TestSpider(scrapy.Spider):
            name = "testspider"

            def parse(self, response):
                yield from items

        TestSpider.start_urls = [self.mockserver.url("/")]
        settings = {
            "FEEDS": {
                f"{feed_dir.as_uri()}/%(time)s.json": {"format": "json"},
            },
        }
        crawler = get_crawler(TestSpider, settings)
        await crawler.crawl_async()

        files = list(feed_dir.iterdir())
        assert len(files) == 1
        assert "%(time)s" not in files[0].name
        assert files[0].suffix == ".json"

    @coroutine_test
    async def test_export_no_items_not_store_empty(self):
        for fmt in ("json", "jsonlines", "xml", "csv"):
            settings = {
                "FEEDS": {
                    self._random_temp_filename(): {"format": fmt},
                },
                "FEED_STORE_EMPTY": False,
            }
            data = await self.exported_no_data(settings)
            assert data[fmt] is None

    @coroutine_test
    async def test_start_finish_exporting_items(self):
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
        ]
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
            },
            "FEED_EXPORT_INDENT": None,
        }

        listener = IsExportingListener()
        InstrumentedFeedSlot.subscribe__listener(listener)

        with mock.patch("scrapy.extensions.feedexport.FeedSlot", InstrumentedFeedSlot):
            await self.exported_data(items, settings)
            assert not listener.start_without_finish
            assert not listener.finish_without_start

    @coroutine_test
    async def test_start_finish_exporting_no_items(self):
        items: list[Any] = []
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
            },
            "FEED_EXPORT_INDENT": None,
        }

        listener = IsExportingListener()
        InstrumentedFeedSlot.subscribe__listener(listener)

        with mock.patch("scrapy.extensions.feedexport.FeedSlot", InstrumentedFeedSlot):
            await self.exported_data(items, settings)
            assert not listener.start_without_finish
            assert not listener.finish_without_start

    @coroutine_test
    async def test_start_finish_exporting_items_exception(self):
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
        ]
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
            },
            "FEED_EXPORTERS": {"json": ExceptionJsonItemExporter},
            "FEED_EXPORT_INDENT": None,
        }

        listener = IsExportingListener()
        InstrumentedFeedSlot.subscribe__listener(listener)

        with mock.patch("scrapy.extensions.feedexport.FeedSlot", InstrumentedFeedSlot):
            await self.exported_data(items, settings)
            assert not listener.start_without_finish
            assert not listener.finish_without_start

    @coroutine_test
    async def test_start_finish_exporting_no_items_exception(self):
        items: list[Any] = []
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
            },
            "FEED_EXPORTERS": {"json": ExceptionJsonItemExporter},
            "FEED_EXPORT_INDENT": None,
        }

        listener = IsExportingListener()
        InstrumentedFeedSlot.subscribe__listener(listener)

        with mock.patch("scrapy.extensions.feedexport.FeedSlot", InstrumentedFeedSlot):
            await self.exported_data(items, settings)
            assert not listener.start_without_finish
            assert not listener.finish_without_start

    @coroutine_test
    async def test_export_no_items_store_empty(self):
        formats = (
            ("json", b"[]"),
            ("jsonlines", b""),
            ("xml", b'<?xml version="1.0" encoding="utf-8"?>\n<items></items>'),
            ("csv", b""),
        )

        for fmt, expctd in formats:
            settings = {
                "FEEDS": {
                    self._random_temp_filename(): {"format": fmt},
                },
                "FEED_STORE_EMPTY": True,
                "FEED_EXPORT_INDENT": None,
            }
            data = await self.exported_no_data(settings)
            assert expctd == data[fmt]

    @coroutine_test
    async def test_export_no_items_multiple_feeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Make sure that `storage.store` is not called."""
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
                self._random_temp_filename(): {"format": "xml"},
                self._random_temp_filename(): {"format": "csv"},
            },
            "FEED_STORAGES": {"file": LogOnStoreFileStorage},
            "FEED_STORE_EMPTY": False,
        }

        with caplog.at_level(logging.INFO):
            await self.exported_no_data(settings)

        assert caplog.text.count("Storage.store is called") == 0

    @coroutine_test
    async def test_export_multiple_item_classes(self):
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
            MyItem2({"hello": "world2", "foo": "bar2"}),
            MyItem({"foo": "bar3", "egg": "spam3", "baz": "quux3"}),
            {"hello": "world4", "egg": "spam4"},
        ]

        # by default, Scrapy uses fields of the first Item for CSV and
        # all fields for JSON Lines
        header = MyItem.fields.keys()
        rows_csv = [
            {"egg": "spam1", "foo": "bar1", "baz": ""},
            {"egg": "", "foo": "bar2", "baz": ""},
            {"egg": "spam3", "foo": "bar3", "baz": "quux3"},
            {"egg": "spam4", "foo": "", "baz": ""},
        ]
        rows_jl = [dict(row) for row in items]
        await self.assertExportedCsv(items, header, rows_csv)
        await self.assertExportedJsonLines(items, rows_jl)

    @coroutine_test
    async def test_export_items_empty_field_list(self):
        # FEED_EXPORT_FIELDS==[] means the same as default None
        items = [{"foo": "bar"}]
        header = ["foo"]
        rows = [{"foo": "bar"}]
        settings: dict[str, Any] = {"FEED_EXPORT_FIELDS": []}
        await self.assertExportedCsv(items, header, rows)
        await self.assertExportedJsonLines(items, rows, settings)

    @coroutine_test
    async def test_export_items_field_list(self):
        items = [{"foo": "bar"}]
        header = ["foo", "baz"]
        rows = [{"foo": "bar", "baz": ""}]
        settings = {"FEED_EXPORT_FIELDS": header}
        await self.assertExported(items, header, rows, settings=settings)

    @coroutine_test
    async def test_export_items_comma_separated_field_list(self):
        items = [{"foo": "bar"}]
        header = ["foo", "baz"]
        rows = [{"foo": "bar", "baz": ""}]
        settings = {"FEED_EXPORT_FIELDS": ",".join(header)}
        await self.assertExported(items, header, rows, settings=settings)

    @coroutine_test
    async def test_export_items_json_field_list(self):
        items = [{"foo": "bar"}]
        header = ["foo", "baz"]
        rows = [{"foo": "bar", "baz": ""}]
        settings = {"FEED_EXPORT_FIELDS": json.dumps(header)}
        await self.assertExported(items, header, rows, settings=settings)

    @coroutine_test
    async def test_export_items_field_names(self):
        items = [{"foo": "bar"}]
        header = {"foo": "Foo"}
        rows = [{"Foo": "bar"}]
        settings = {"FEED_EXPORT_FIELDS": header}
        await self.assertExported(items, list(header.values()), rows, settings=settings)

    @coroutine_test
    async def test_export_items_dict_field_names(self):
        items = [{"foo": "bar"}]
        header = {
            "baz": "Baz",
            "foo": "Foo",
        }
        rows = [{"Baz": "", "Foo": "bar"}]
        settings = {"FEED_EXPORT_FIELDS": header}
        await self.assertExported(items, ["Baz", "Foo"], rows, settings=settings)

    @coroutine_test
    async def test_export_items_json_field_names(self):
        items = [{"foo": "bar"}]
        header = {"foo": "Foo"}
        rows = [{"Foo": "bar"}]
        settings = {"FEED_EXPORT_FIELDS": json.dumps(header)}
        await self.assertExported(items, list(header.values()), rows, settings=settings)

    @coroutine_test
    async def test_export_based_on_item_classes(self):
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
            MyItem2({"hello": "world2", "foo": "bar2"}),
            {"hello": "world3", "egg": "spam3"},
        ]

        formats = {
            "csv": b"foo,egg,baz\r\nbar1,spam1,\r\n",
            "json": b'[\n{"foo": "bar2", "hello": "world2"}\n]',
            "jsonlines": (
                b'{"foo": "bar1", "egg": "spam1"}\n{"foo": "bar2", "hello": "world2"}\n'
            ),
            "xml": (
                b'<?xml version="1.0" encoding="utf-8"?>\n<items>\n<item>'
                b"<foo>bar1</foo><egg>spam1</egg></item>\n<item><foo>"
                b"bar2</foo><hello>world2</hello></item>\n<item><hello>world3"
                b"</hello><egg>spam3</egg></item>\n</items>"
            ),
        }

        settings = {
            "FEEDS": {
                self._random_temp_filename(): {
                    "format": "csv",
                    "item_classes": [MyItem],
                },
                self._random_temp_filename(): {
                    "format": "json",
                    "item_classes": [MyItem2],
                },
                self._random_temp_filename(): {
                    "format": "jsonlines",
                    "item_classes": [MyItem, MyItem2],
                },
                self._random_temp_filename(): {
                    "format": "xml",
                },
            },
        }

        data = await self.exported_data(items, settings)
        for fmt, expected in formats.items():
            assert data[fmt] == expected

    @coroutine_test
    async def test_export_based_on_custom_filters(self):
        items = [
            MyItem({"foo": "bar1", "egg": "spam1"}),
            MyItem2({"hello": "world2", "foo": "bar2"}),
            {"hello": "world3", "egg": "spam3"},
        ]

        class CustomFilter1:
            def __init__(self, feed_options):
                pass

            def accepts(self, item):
                return isinstance(item, MyItem)

        class CustomFilter2(ItemFilter):
            def accepts(self, item):
                return "foo" in item.fields

        class CustomFilter3(ItemFilter):
            def accepts(self, item):
                return (
                    isinstance(item, tuple(self.item_classes)) and item["foo"] == "bar1"  # type: ignore[index]
                )

        formats = {
            "json": b'[\n{"foo": "bar1", "egg": "spam1"}\n]',
            "xml": (
                b'<?xml version="1.0" encoding="utf-8"?>\n<items>\n<item>'
                b"<foo>bar1</foo><egg>spam1</egg></item>\n<item><foo>"
                b"bar2</foo><hello>world2</hello></item>\n</items>"
            ),
            "jsonlines": b'{"foo": "bar1", "egg": "spam1"}\n',
        }

        settings = {
            "FEEDS": {
                self._random_temp_filename(): {
                    "format": "json",
                    "item_filter": CustomFilter1,
                },
                self._random_temp_filename(): {
                    "format": "xml",
                    "item_filter": CustomFilter2,
                },
                self._random_temp_filename(): {
                    "format": "jsonlines",
                    "item_classes": [MyItem, MyItem2],
                    "item_filter": CustomFilter3,
                },
            },
        }

        data = await self.exported_data(items, settings)
        for fmt, expected in formats.items():
            assert data[fmt] == expected

    @coroutine_test
    async def test_export_dicts(self):
        # When dicts are used, only keys from the first row are used as
        # a header for CSV, and all fields are used for JSON Lines.
        items = [
            {"foo": "bar", "egg": "spam"},
            {"foo": "bar", "egg": "spam", "baz": "quux"},
        ]
        rows_csv = [{"egg": "spam", "foo": "bar"}, {"egg": "spam", "foo": "bar"}]
        rows_jl = items
        await self.assertExportedCsv(items, ["foo", "egg"], rows_csv)
        await self.assertExportedJsonLines(items, rows_jl)

    @coroutine_test
    async def test_export_tuple(self):
        items = [
            {"foo": "bar1", "egg": "spam1"},
            {"foo": "bar2", "egg": "spam2", "baz": "quux"},
        ]

        settings = {"FEED_EXPORT_FIELDS": ("foo", "baz")}
        rows = [{"foo": "bar1", "baz": ""}, {"foo": "bar2", "baz": "quux"}]
        await self.assertExported(items, ["foo", "baz"], rows, settings=settings)

    @coroutine_test
    async def test_export_feed_export_fields(self):
        # FEED_EXPORT_FIELDS option allows to order export fields
        # and to select a subset of fields to export, both for Items and dicts.

        for item_cls in [MyItem, dict]:
            items = [
                item_cls({"foo": "bar1", "egg": "spam1"}),
                item_cls({"foo": "bar2", "egg": "spam2", "baz": "quux2"}),
            ]

            # export all columns
            settings = {"FEED_EXPORT_FIELDS": "foo,baz,egg"}
            rows = [
                {"egg": "spam1", "foo": "bar1", "baz": ""},
                {"egg": "spam2", "foo": "bar2", "baz": "quux2"},
            ]
            await self.assertExported(
                items, ["foo", "baz", "egg"], rows, settings=settings
            )

            # export a subset of columns
            settings = {"FEED_EXPORT_FIELDS": "egg,baz"}
            rows = [{"egg": "spam1", "baz": ""}, {"egg": "spam2", "baz": "quux2"}]
            await self.assertExported(items, ["egg", "baz"], rows, settings=settings)

    @coroutine_test
    async def test_export_encoding(self):
        items = [{"foo": "Test\xd6"}]

        formats = {
            "json": b'[{"foo": "Test\\u00d6"}]',
            "jsonlines": b'{"foo": "Test\\u00d6"}\n',
            "xml": (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                "<items><item><foo>Test\xd6</foo></item></items>"
            ).encode(),
            "csv": "foo\r\nTest\xd6\r\n".encode(),
        }

        for fmt, expected in formats.items():
            settings: dict[str, Any] = {
                "FEEDS": {
                    self._random_temp_filename(): {"format": fmt},
                },
                "FEED_EXPORT_INDENT": None,
            }
            data = await self.exported_data(items, settings)
            assert data[fmt] == expected

        formats = {
            "json": b'[{"foo": "Test\xd6"}]',
            "jsonlines": b'{"foo": "Test\xd6"}\n',
            "xml": (
                b'<?xml version="1.0" encoding="latin-1"?>\n'
                b"<items><item><foo>Test\xd6</foo></item></items>"
            ),
            "csv": b"foo\r\nTest\xd6\r\n",
        }

        for fmt, expected in formats.items():
            settings = {
                "FEEDS": {
                    self._random_temp_filename(): {"format": fmt},
                },
                "FEED_EXPORT_INDENT": None,
                "FEED_EXPORT_ENCODING": "latin-1",
            }
            data = await self.exported_data(items, settings)
            assert data[fmt] == expected

    @coroutine_test
    async def test_export_multiple_configs(self):
        items = [{"foo": "FOO", "bar": "BAR"}]

        formats = {
            "json": b'[\n{"bar": "BAR"}\n]',
            "xml": (
                b'<?xml version="1.0" encoding="latin-1"?>\n'
                b"<items>\n  <item>\n    <foo>FOO</foo>\n  </item>\n</items>"
            ),
            "csv": b"bar,foo\r\nBAR,FOO\r\n",
        }

        settings = {
            "FEEDS": {
                self._random_temp_filename(): {
                    "format": "json",
                    "indent": 0,
                    "fields": ["bar"],
                    "encoding": "utf-8",
                },
                self._random_temp_filename(): {
                    "format": "xml",
                    "indent": 2,
                    "fields": ["foo"],
                    "encoding": "latin-1",
                },
                self._random_temp_filename(): {
                    "format": "csv",
                    "indent": None,
                    "fields": ["bar", "foo"],
                    "encoding": "utf-8",
                },
            },
        }

        data = await self.exported_data(items, settings)
        for fmt, expected in formats.items():
            assert data[fmt] == expected

    @coroutine_test
    async def test_export_indentation(self):
        items = [
            {"foo": ["bar"]},
            {"key": "value"},
        ]

        test_cases: list[dict[str, Any]] = [
            # JSON
            {
                "format": "json",
                "indent": None,
                "expected": b'[{"foo": ["bar"]},{"key": "value"}]',
            },
            {
                "format": "json",
                "indent": -1,
                "expected": b"""[
{"foo": ["bar"]},
{"key": "value"}
]""",
            },
            {
                "format": "json",
                "indent": 0,
                "expected": b"""[
{"foo": ["bar"]},
{"key": "value"}
]""",
            },
            {
                "format": "json",
                "indent": 2,
                "expected": b"""[
{
  "foo": [
    "bar"
  ]
},
{
  "key": "value"
}
]""",
            },
            {
                "format": "json",
                "indent": 4,
                "expected": b"""[
{
    "foo": [
        "bar"
    ]
},
{
    "key": "value"
}
]""",
            },
            {
                "format": "json",
                "indent": 5,
                "expected": b"""[
{
     "foo": [
          "bar"
     ]
},
{
     "key": "value"
}
]""",
            },
            # XML
            {
                "format": "xml",
                "indent": None,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items><item><foo><value>bar</value></foo></item><item><key>value</key></item></items>""",
            },
            {
                "format": "xml",
                "indent": -1,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items>
<item><foo><value>bar</value></foo></item>
<item><key>value</key></item>
</items>""",
            },
            {
                "format": "xml",
                "indent": 0,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items>
<item><foo><value>bar</value></foo></item>
<item><key>value</key></item>
</items>""",
            },
            {
                "format": "xml",
                "indent": 2,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items>
  <item>
    <foo>
      <value>bar</value>
    </foo>
  </item>
  <item>
    <key>value</key>
  </item>
</items>""",
            },
            {
                "format": "xml",
                "indent": 4,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items>
    <item>
        <foo>
            <value>bar</value>
        </foo>
    </item>
    <item>
        <key>value</key>
    </item>
</items>""",
            },
            {
                "format": "xml",
                "indent": 5,
                "expected": b"""<?xml version="1.0" encoding="utf-8"?>
<items>
     <item>
          <foo>
               <value>bar</value>
          </foo>
     </item>
     <item>
          <key>value</key>
     </item>
</items>""",
            },
        ]

        for row in test_cases:
            settings = {
                "FEEDS": {
                    self._random_temp_filename(): {
                        "format": row["format"],
                        "indent": row["indent"],
                    },
                },
            }
            data = await self.exported_data(items, settings)
            assert data[row["format"]] == row["expected"]

    @coroutine_test
    async def test_init_exporters_storages_with_crawler(self):
        settings = {
            "FEED_EXPORTERS": {"csv": FromCrawlerCsvItemExporter},
            "FEED_STORAGES": {"file": FromCrawlerFileFeedStorage},
            "FEEDS": {
                self._random_temp_filename(): {"format": "csv"},
            },
        }
        await self.exported_data(items=[], settings=settings)
        assert FromCrawlerCsvItemExporter.init_with_crawler
        assert FromCrawlerFileFeedStorage.init_with_crawler

    @coroutine_test
    async def test_str_uri(self):
        settings = {
            "FEED_STORE_EMPTY": True,
            "FEEDS": {str(self._random_temp_filename()): {"format": "csv"}},
        }
        data = await self.exported_no_data(settings)
        assert data["csv"] == b""

    @coroutine_test
    async def test_multiple_feeds_success_logs_blocking_feed_storage(
        self, caplog: pytest.LogCaptureFixture
    ):
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
                self._random_temp_filename(): {"format": "xml"},
                self._random_temp_filename(): {"format": "csv"},
            },
            "FEED_STORAGES": {"file": DummyBlockingFeedStorage},
        }
        items = [
            {"foo": "bar1", "baz": ""},
            {"foo": "bar2", "baz": "quux"},
        ]
        with caplog.at_level(logging.DEBUG):
            await self.exported_data(items, settings)

        for fmt in ["json", "xml", "csv"]:
            assert f"Stored {fmt} feed (2 items)" in caplog.text

    @coroutine_test
    async def test_multiple_feeds_failing_logs_blocking_feed_storage(
        self, caplog: pytest.LogCaptureFixture
    ):
        settings = {
            "FEEDS": {
                self._random_temp_filename(): {"format": "json"},
                self._random_temp_filename(): {"format": "xml"},
                self._random_temp_filename(): {"format": "csv"},
            },
            "FEED_STORAGES": {"file": FailingBlockingFeedStorage},
        }
        items = [
            {"foo": "bar1", "baz": ""},
            {"foo": "bar2", "baz": "quux"},
        ]
        with caplog.at_level(logging.DEBUG):
            await self.exported_data(items, settings)

        for fmt in ["json", "xml", "csv"]:
            assert f"Error storing {fmt} feed (2 items)" in caplog.text

    @coroutine_test
    async def test_extend_kwargs(self):
        items = [{"foo": "FOO", "bar": "BAR"}]

        expected_with_title_csv = b"foo,bar\r\nFOO,BAR\r\n"
        expected_without_title_csv = b"FOO,BAR\r\n"
        test_cases: list[dict[str, Any]] = [
            # with title
            {
                "options": {
                    "format": "csv",
                    "item_export_kwargs": {"include_headers_line": True},
                },
                "expected": expected_with_title_csv,
            },
            # without title
            {
                "options": {
                    "format": "csv",
                    "item_export_kwargs": {"include_headers_line": False},
                },
                "expected": expected_without_title_csv,
            },
        ]

        for row in test_cases:
            feed_options = row["options"]
            settings = {
                "FEEDS": {
                    self._random_temp_filename(): feed_options,
                },
                "FEED_EXPORT_INDENT": None,
            }

            data = await self.exported_data(items, settings)
            assert data[feed_options["format"]] == row["expected"]

    @coroutine_test
    async def test_storage_file_no_postprocessing(self):
        class Storage:
            open_file: IO[bytes]
            store_file: IO[bytes]

            def __init__(self, uri, *, feed_options=None):
                pass

            def open(self, spider):
                Storage.open_file = tempfile.NamedTemporaryFile(prefix="feed-")
                return Storage.open_file

            def store(self, file):
                Storage.store_file = file
                file.close()

        settings = {
            "FEEDS": {self._random_temp_filename(): {"format": "jsonlines"}},
            "FEED_STORAGES": {"file": Storage},
        }
        await self.exported_no_data(settings)
        assert Storage.open_file is Storage.store_file

    @coroutine_test
    async def test_storage_file_postprocessing(self):
        class Storage:
            open_file: IO[bytes]
            store_file: IO[bytes]
            file_was_closed: bool

            def __init__(self, uri, *, feed_options=None):
                pass

            def open(self, spider):
                Storage.open_file = tempfile.NamedTemporaryFile(prefix="feed-")
                return Storage.open_file

            def store(self, file):
                Storage.store_file = file
                Storage.file_was_closed = file.closed
                file.close()

        settings = {
            "FEEDS": {
                self._random_temp_filename(): {
                    "format": "jsonlines",
                    "postprocessing": [
                        "scrapy.extensions.postprocessing.GzipPlugin",
                    ],
                },
            },
            "FEED_STORAGES": {"file": Storage},
        }
        await self.exported_no_data(settings)
        assert Storage.open_file is Storage.store_file
        assert not Storage.file_was_closed


# Test that the FeedExporer sends the feed_exporter_closed and feed_slot_closed signals
class TestFeedExporterSignals:
    items = [
        {"foo": "bar1", "egg": "spam1"},
        {"foo": "bar2", "egg": "spam2", "baz": "quux2"},
        {"foo": "bar3", "baz": "quux3"},
    ]

    with tempfile.NamedTemporaryFile(suffix="json") as tmp:
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(tmp.name)): {
                    "format": "json",
                },
            },
        }

    def feed_exporter_closed_signal_handler(self):
        self.feed_exporter_closed_received = True

    def feed_slot_closed_signal_handler(self, slot):
        self.feed_slot_closed_received = True

    async def feed_exporter_closed_signal_handler_async(self):
        self.feed_exporter_closed_received = True

    async def feed_slot_closed_signal_handler_async(self, slot):
        self.feed_slot_closed_received = True

    async def run_signaled_feed_exporter(
        self,
        feed_exporter_signal_handler: Callable[[], Awaitable[None] | None],
        feed_slot_signal_handler: Callable[[Any], Awaitable[None] | None],
    ) -> None:
        crawler = get_crawler(settings_dict=self.settings)
        feed_exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        crawler.signals.connect(
            feed_exporter_signal_handler,
            signal=signals.feed_exporter_closed,
        )
        crawler.signals.connect(
            feed_slot_signal_handler, signal=signals.feed_slot_closed
        )
        feed_exporter.open_spider(spider)
        for item in self.items:
            feed_exporter.item_scraped(item, spider)
        await feed_exporter.close_spider(spider)

    @coroutine_test
    async def test_feed_exporter_signals_sent(self) -> None:
        self.feed_exporter_closed_received = False
        self.feed_slot_closed_received = False

        await self.run_signaled_feed_exporter(
            self.feed_exporter_closed_signal_handler,
            self.feed_slot_closed_signal_handler,
        )
        assert self.feed_slot_closed_received
        assert self.feed_exporter_closed_received

    @coroutine_test
    async def test_feed_exporter_signals_sent_async(self) -> None:
        self.feed_exporter_closed_received = False
        self.feed_slot_closed_received = False

        await self.run_signaled_feed_exporter(
            self.feed_exporter_closed_signal_handler_async,
            self.feed_slot_closed_signal_handler_async,
        )
        assert self.feed_slot_closed_received
        assert self.feed_exporter_closed_received


class TestItemFilter:
    def test_no_feed_options(self):
        item_filter = ItemFilter(None)
        assert item_filter.item_classes == ()
        assert item_filter.accepts(MyItem({"foo": "bar"}))


class TestFeedExportInit:
    def test_unsupported_storage(self):
        settings: dict[str, Any] = {
            "FEEDS": {
                "unsupported://uri": {},
            },
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)

    def test_disabled_storage(self, caplog: pytest.LogCaptureFixture):
        class DisabledFeedStorage:
            def __init__(self, uri, *, feed_options=None):
                raise NotConfigured("not today")

        settings = {
            "FEED_STORAGES": {"disabled": DisabledFeedStorage},
            "FEEDS": {
                "disabled://uri": {},
            },
        }
        crawler = get_crawler(settings_dict=settings)
        with caplog.at_level(logging.ERROR), pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)
        assert (
            "Disabled feed storage scheme: disabled. Reason: not today" in caplog.text
        )

    def test_unsupported_format(self):
        settings = {
            "FEEDS": {
                "file://path": {
                    "format": "unsupported_format",
                },
            },
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)

    def test_format_inferred_from_uri(self):
        settings: dict[str, Any] = {
            "FEEDS": {
                "output.json": {},
            },
        }
        crawler = get_crawler(settings_dict=settings)
        exporter = build_from_crawler(FeedExporter, crawler)
        assert exporter.feeds["output.json"]["format"] == "json"

    def test_format_missing_and_not_inferable(self, caplog: pytest.LogCaptureFixture):
        settings: dict[str, Any] = {
            "FEEDS": {
                "stdout:": {},
            },
        }
        crawler = get_crawler(settings_dict=settings)
        with caplog.at_level(logging.ERROR), pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)
        assert "Feed format not set" in caplog.text

    def test_absolute_pathlib_as_uri(self):
        with tempfile.NamedTemporaryFile(suffix="json") as tmp:
            settings = {
                "FEEDS": {
                    Path(tmp.name).resolve(): {
                        "format": "json",
                    },
                },
            }
            crawler = get_crawler(settings_dict=settings)
            exporter = build_from_crawler(FeedExporter, crawler)
            assert isinstance(exporter, FeedExporter)

    def test_relative_pathlib_as_uri(self):
        settings = {
            "FEEDS": {
                Path("./items.json"): {
                    "format": "json",
                },
            },
        }
        crawler = get_crawler(settings_dict=settings)
        exporter = build_from_crawler(FeedExporter, crawler)
        assert isinstance(exporter, FeedExporter)


_CRASH_RESUME_SCRIPT = """
import asyncio
import os
import sys

import scrapy
from scrapy.extensions.feedexport import FileFeedStorage
from scrapy.utils.test import get_crawler

out_dir = sys.argv[1]
crash = sys.argv[2] == "1"


class CrashStorage(FileFeedStorage):
    def store(self, file):
        super().store(file)
        if crash and self.path.endswith("batch-2.jl.gz"):
            os._exit(1)


class TestSpider(scrapy.Spider):
    name = "testspider"

    async def start(self):
        for i in range(5):
            yield {"i": i}


async def main():
    crawler = get_crawler(
        TestSpider,
        {
            "FEEDS": {
                os.path.join(out_dir, "batch-%(batch_id)d.jl.gz"): {
                    "format": "jl",
                    "batch_item_count": 2,
                    "manifest": True,
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                }
            },
            "FEED_STORAGES": {"": CrashStorage, "file": CrashStorage},
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        },
    )
    await crawler.crawl_async()


asyncio.run(main())
"""


class TestFeedExportManifest:
    """Tests for the ``manifest`` feed option."""

    @staticmethod
    def _spider_cls(item_count: int = 5) -> type[Spider]:
        class TestSpider(scrapy.Spider):
            name = "testspider"

            async def start(self):
                for i in range(item_count):
                    yield {"i": i}

        return TestSpider

    async def _crawl(self, settings: dict[str, Any], item_count: int = 5) -> None:
        crawler = get_crawler(self._spider_cls(item_count), settings)
        await crawler.crawl_async()

    @staticmethod
    def _settings(
        feed_dir: Path, template: str = "batch-%(batch_id)d.jl", **feed_options: Any
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": "jl",
            "batch_item_count": 2,
            "manifest": True,
        }
        options.update(feed_options)
        return {"FEEDS": {str(feed_dir / template): options}}

    @staticmethod
    def _read_manifest(
        manifest_path: Path,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]

    @staticmethod
    def _assert_committed_files(
        manifest_path: Path, entries: list[dict[str, Any]]
    ) -> None:
        for entry in entries:
            file_path = manifest_path.parent / entry["file"]
            assert file_path.is_file()
            assert file_path.stat().st_size == entry["bytes"]
            assert hashlib.sha256(file_path.read_bytes()).hexdigest() == entry["sha256"]

    async def test_committed_batches(self, tmp_path: Path) -> None:
        settings = self._settings(
            tmp_path,
            "batch-%(batch_id)d.jl.gz",
            fields=("i",),
            postprocessing=[GzipPlugin],
        )
        await self._crawl(settings)
        manifest_path = tmp_path / "batch-.jl.gz.manifest.jsonl"
        header, entries = self._read_manifest(manifest_path)
        assert header["uri_template"] == str(tmp_path / "batch-%(batch_id)d.jl.gz")
        assert header["config"]["postprocessing"] == [str(GzipPlugin)]
        assert [entry["batch_id"] for entry in entries] == [1, 2, 3]
        assert [entry["item_count"] for entry in entries] == [2, 2, 1]
        self._assert_committed_files(manifest_path, entries)
        items: list[dict[str, Any]] = []
        for entry in entries:
            with gzip.open(manifest_path.parent / entry["file"], "rt") as file:
                items.extend(json.loads(line) for line in file)
        assert items == [{"i": i} for i in range(5)]

    async def test_multiple_feeds(self, tmp_path: Path) -> None:
        settings = {
            "FEEDS": {
                str(tmp_path / "a" / "batch-%(batch_id)d.jl"): {
                    "format": "jl",
                    "batch_item_count": 2,
                    "manifest": True,
                },
                path_to_url(tmp_path / "b") + "/batch-%(batch_id)d.jl": {
                    "format": "jl",
                    "batch_item_count": 3,
                    "manifest": True,
                },
            }
        }
        await self._crawl(settings)
        manifest_a = tmp_path / "a" / "batch-.jl.manifest.jsonl"
        _, entries_a = self._read_manifest(manifest_a)
        assert [entry["batch_id"] for entry in entries_a] == [1, 2, 3]
        self._assert_committed_files(manifest_a, entries_a)
        manifest_b = tmp_path / "b" / "batch-.jl.manifest.jsonl"
        _, entries_b = self._read_manifest(manifest_b)
        assert [entry["batch_id"] for entry in entries_b] == [1, 2]
        self._assert_committed_files(manifest_b, entries_b)

    def test_requires_batch_item_count(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path, batch_item_count=0)
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)

    def test_requires_batch_id_placeholder(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path, "batch-%(batch_time)s.jl")
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)

    def test_requires_file_storage(self) -> None:
        settings = {
            "FEEDS": {
                "stdout:%(batch_id)d": {
                    "format": "jl",
                    "batch_item_count": 2,
                    "manifest": True,
                }
            }
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)

    async def test_failed_batch_is_not_committed(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        settings["FEED_STORAGES"] = {
            "": FailingBatchFileStorage,
            "file": FailingBatchFileStorage,
        }
        await self._crawl(settings)
        manifest_path = tmp_path / "batch-.jl.manifest.jsonl"
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1, 3]
        self._assert_committed_files(manifest_path, entries)
        # The file of the failed batch exists but is not committed.
        assert (tmp_path / "batch-2.jl").is_file()

    async def test_resume_continues_with_unused_batch_ids(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        manifest_path = tmp_path / "batch-.jl.manifest.jsonl"
        digests = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.glob("batch-*.jl")
        }
        await self._crawl(settings, item_count=3)
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1, 2, 3, 4]
        assert [entry["item_count"] for entry in entries] == [2, 2, 2, 1]
        self._assert_committed_files(manifest_path, entries)
        # Batches committed by the first run were not overwritten.
        for name, digest in digests.items():
            assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest

    async def test_resume_removes_uncommitted_files(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        stale = tmp_path / "batch-3.jl"
        stale.write_bytes(b'{"stale": 1}\n')
        await self._crawl(settings, item_count=1)
        # The stale leftover was removed, not appended to.
        assert stale.read_bytes() == b'{"i": 0}\n'
        manifest_path = tmp_path / "batch-.jl.manifest.jsonl"
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1, 2, 3]
        self._assert_committed_files(manifest_path, entries)

    async def test_resume_keeps_manifest_file(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path, "batch-%(batch_id)d")
        await self._crawl(settings, item_count=2)
        manifest_path = tmp_path / "batch-.manifest.jsonl"
        assert manifest_path.is_file()
        await self._crawl(settings, item_count=2)
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1, 2]

    async def test_resume_detects_missing_file(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        (tmp_path / "batch-1.jl").unlink()
        with pytest.raises(ValueError, match="batch 1"):
            get_crawler(self._spider_cls(), settings)

    async def test_resume_detects_resized_file(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        with (tmp_path / "batch-2.jl").open("ab") as file:
            file.write(b"x")
        with pytest.raises(ValueError, match="batch 2"):
            get_crawler(self._spider_cls(), settings)

    async def test_resume_detects_modified_file(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        path = tmp_path / "batch-2.jl"
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
        with pytest.raises(ValueError, match="batch 2"):
            get_crawler(self._spider_cls(), settings)

    async def test_resume_detects_feed_option_change(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=4)
        new_settings = self._settings(tmp_path, batch_item_count=3)
        with pytest.raises(ValueError, match="different feed options"):
            get_crawler(self._spider_cls(), new_settings)

    async def test_resume_detects_uri_template_change(self, tmp_path: Path) -> None:
        settings = self._settings(tmp_path)
        await self._crawl(settings, item_count=2)
        new_settings = self._settings(tmp_path, "batch-%(batch_id)02d.jl")
        with pytest.raises(ValueError, match="belongs to"):
            get_crawler(self._spider_cls(), new_settings)

    @pytest.mark.parametrize("content", ["", "not json\n"])
    def test_invalid_manifest(self, tmp_path: Path, content: str) -> None:
        (tmp_path / "batch-.jl.manifest.jsonl").write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid feed manifest"):
            get_crawler(self._spider_cls(), self._settings(tmp_path))

    def test_commit_failure_removes_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = _FeedManifest(str(tmp_path / "batch-%(batch_id)d.jl"), {})
        batch_file = tmp_path / "batch-1.jl"
        batch_file.write_bytes(b"{}\n")

        def failing_replace(src: str, dst: str) -> None:
            raise OSError("cannot replace")

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError, match="cannot replace"):
            manifest.commit(1, str(batch_file), 1)
        assert not list(tmp_path.glob("*.tmp"))

    def test_crash_before_commit_then_resume(self, tmp_path: Path) -> None:
        script_path = tmp_path / "crash_resume.py"
        script_path.write_text(_CRASH_RESUME_SCRIPT, encoding="utf-8")
        out_dir = tmp_path / "out"
        command = [sys.executable, str(script_path), str(out_dir)]

        crash = subprocess.run(
            [*command, "1"], check=False, capture_output=True, text=True
        )
        assert crash.returncode == 1, crash.stderr
        manifest_path = out_dir / "batch-.jl.gz.manifest.jsonl"
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1]
        # The batch 2 file was fully written, but the process was killed
        # before it could be committed to the manifest.
        assert (out_dir / "batch-2.jl.gz").is_file()
        batch_1_sha256 = hashlib.sha256(
            (out_dir / "batch-1.jl.gz").read_bytes()
        ).hexdigest()

        resume = subprocess.run(
            [*command, "0"], check=False, capture_output=True, text=True
        )
        assert resume.returncode == 0, resume.stderr
        _, entries = self._read_manifest(manifest_path)
        assert [entry["batch_id"] for entry in entries] == [1, 2, 3, 4]
        assert [entry["item_count"] for entry in entries] == [2, 2, 2, 1]
        self._assert_committed_files(manifest_path, entries)
        # The batch committed before the crash was not overwritten.
        assert (
            hashlib.sha256((out_dir / "batch-1.jl.gz").read_bytes()).hexdigest()
            == batch_1_sha256
        )
        # The uncommitted leftover was replaced: batch 2 now holds the
        # re-scraped first two items instead of the stale items 2 and 3.
        with gzip.open(out_dir / "batch-2.jl.gz", "rt") as file:
            assert [json.loads(line) for line in file] == [{"i": 0}, {"i": 1}]


class TestApplyUriParams:
    params = {
        "name": "myspider",
        "time": "2020-01-01T00-00-00",
        "batch_id": 2,
        "batch_time": "2020-01-01T00-00-00",
    }

    @pytest.mark.parametrize(
        ("uri_template", "expected"),
        [
            # Placeholders are substituted, including width/flags.
            ("/data/%(name)s/%(time)s.json", "/data/myspider/2020-01-01T00-00-00.json"),
            ("/data/%(batch_id)05d.json", "/data/00002.json"),
            # Percent-encoding is kept verbatim (#6425, #5794).
            (
                "file:///path%20with%20spaces/%(name)s.json",
                "file:///path%20with%20spaces/myspider.json",
            ),
            (
                "ftp://user:2%23um25%21M%23JZ@ftp.example.com/%(name)s.csv",
                "ftp://user:2%23um25%21M%23JZ@ftp.example.com/myspider.csv",
            ),
            # A lone percent character next to a placeholder stays literal.
            ("/100%/%(name)s.json", "/100%/myspider.json"),
        ],
    )
    def test_apply_uri_params(self, uri_template, expected):
        assert apply_uri_params(uri_template, self.params) == expected
