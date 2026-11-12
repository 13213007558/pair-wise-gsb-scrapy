from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest

import scrapy
from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.exporters import CsvItemExporter
from scrapy.extensions.feedexport import FeedExporter, FileFeedStorage
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import (
    export_by_format,
    path_to_url,
    printf_escape,
    unique_path,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.mockserver.http import MockServer


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


@coroutine_test
async def test_init_exporters_storages_with_crawler(
    mockserver: MockServer, tmp_path: Path
) -> None:
    settings = {
        "FEED_EXPORTERS": {"csv": FromCrawlerCsvItemExporter},
        "FEED_STORAGES": {"file": FromCrawlerFileFeedStorage},
        "FEEDS": {
            unique_path(tmp_path): {"format": "csv"},
        },
    }
    await export_by_format(mockserver, [], settings)
    assert FromCrawlerCsvItemExporter.init_with_crawler
    assert FromCrawlerFileFeedStorage.init_with_crawler


@coroutine_test
async def test_str_uri(mockserver: MockServer, tmp_path: Path) -> None:
    settings = {
        "FEED_STORE_EMPTY": True,
        "FEEDS": {str(unique_path(tmp_path)): {"format": "csv"}},
    }
    data = await export_by_format(mockserver, [], settings)
    assert data["csv"] == b""


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
            await feed_exporter.item_scraped(item, spider)
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

    def test_disabled_storage(self, caplog: pytest.LogCaptureFixture) -> None:
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

    def test_format_missing_and_not_inferable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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


class FakeStagedFeed:
    """State of a fake staged feed, shared by the storage instances of all
    its batches."""

    def __init__(self, root: Path) -> None:
        self.staging_dir = root / ".staging"
        self.staging_dir.mkdir()
        self.index_path = root / "index"
        self.shards: list[Path] = []
        self.instances: list[FakeStagedFeedStorage] = []
        self.fail_marker: bytes | None = None
        self.commit_calls = 0
        self.abort_calls = 0
        self.publish_count = 0

    def read_public(self) -> bytes | None:
        """Return the feed contents visible to readers, or None if the index
        has not been published."""
        if not self.index_path.exists():
            return None
        return b"".join(
            (self.staging_dir / name).read_bytes()
            for name in self.index_path.read_text().splitlines()
        )


class FakeStagedFeedStorage:
    """Async feed storage that stages each stored file as a shard in a
    private directory and publishes a readable shard index on commit."""

    feeds: dict[str, FakeStagedFeed] = {}
    supported_modes: frozenset[str] = frozenset({"append", "create", "overwrite"})

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.feed = self.feeds[urlparse(uri).netloc]
        self.shard: Path | None = None
        self.feed.instances.append(self)

    async def open(self, spider: scrapy.Spider) -> IO[bytes]:
        return NamedTemporaryFile(prefix="staged-", dir=self.feed.staging_dir)

    async def store(self, file: IO[bytes]) -> None:
        file.seek(0)
        data = file.read()
        file.close()
        if self.feed.fail_marker and self.feed.fail_marker in data:
            raise OSError("simulated shard upload failure")
        shard = self.feed.staging_dir / f"{uuid4().hex}.shard"
        shard.write_bytes(data)
        self.shard = shard
        self.feed.shards.append(shard)

    async def commit(self) -> None:
        self.feed.commit_calls += 1
        assert self.shard
        published = (
            self.feed.index_path.read_text().splitlines()
            if self.feed.index_path.exists()
            else []
        )
        if self.shard.name in published:
            # Already published, e.g. by a run that crashed after publishing.
            return
        tmp_path = self.feed.index_path.with_suffix(".tmp")
        tmp_path.write_text("\n".join([*published, self.shard.name]) + "\n")
        tmp_path.replace(self.feed.index_path)  # atomic publish
        self.feed.publish_count += 1

    async def abort(self) -> None:
        self.feed.abort_calls += 1
        if self.shard is not None:
            self.shard.unlink(missing_ok=True)


class TestStagedCommit:
    @pytest.fixture
    def feed(self, tmp_path: Path) -> Any:
        feed = FakeStagedFeed(tmp_path)
        FakeStagedFeedStorage.feeds["feed"] = feed
        try:
            yield feed
        finally:
            FakeStagedFeedStorage.feeds.clear()

    @staticmethod
    def build_exporter(
        uri: str, feed_options: dict[str, Any]
    ) -> tuple[FeedExporter, scrapy.Spider]:
        settings = {
            "FEED_STORAGES": {"staged": FakeStagedFeedStorage},
            "FEEDS": {uri: {"format": "json", "staged_commit": True, **feed_options}},
        }
        crawler = get_crawler(settings_dict=settings)
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        return exporter, spider

    @coroutine_test
    async def test_uncommitted_shards_not_public(self, feed: FakeStagedFeed) -> None:
        exporter, spider = self.build_exporter(
            "staged://feed/%(batch_id)d", {"batch_item_count": 1}
        )
        exporter.open_spider(spider)
        await exporter.item_scraped({"foo": "bar1"}, spider)
        for _ in range(3):  # let the scheduled close of the first batch run
            await asyncio.sleep(0)
        assert len(feed.shards) == 1  # the first shard is staged...
        assert feed.read_public() is None  # ...but not publicly readable
        await exporter.item_scraped({"foo": "bar2"}, spider)
        await exporter.close_spider(spider)
        data = feed.read_public()
        assert data is not None
        assert b"bar1" in data
        assert b"bar2" in data
        assert feed.commit_calls == 2
        assert feed.abort_calls == 0

    @coroutine_test
    async def test_failed_shard_aborts_staged_shards(
        self, feed: FakeStagedFeed
    ) -> None:
        feed.fail_marker = b"bad"
        exporter, spider = self.build_exporter(
            "staged://feed/%(batch_id)d", {"batch_item_count": 1}
        )
        exporter.open_spider(spider)
        await exporter.item_scraped({"foo": "good"}, spider)
        await exporter.item_scraped({"foo": "bad"}, spider)
        await exporter.close_spider(spider)
        assert feed.commit_calls == 0
        assert feed.abort_calls == 1
        assert not feed.shards[0].exists()  # the staged shard was discarded
        assert feed.read_public() is None

    @coroutine_test
    async def test_empty_output_publishes_nothing(self, feed: FakeStagedFeed) -> None:
        exporter, spider = self.build_exporter(
            "staged://feed", {"store_empty": False}
        )
        exporter.open_spider(spider)
        await exporter.close_spider(spider)
        assert feed.commit_calls == 0
        assert feed.abort_calls == 0
        assert feed.read_public() is None

    @coroutine_test
    async def test_recovery_does_not_republish(self, feed: FakeStagedFeed) -> None:
        exporter, spider = self.build_exporter("staged://feed", {})
        exporter.open_spider(spider)
        await exporter.item_scraped({"foo": "bar"}, spider)
        await exporter.close_spider(spider)
        assert feed.publish_count == 1
        index = feed.index_path.read_text()
        # A retried commit, e.g. by a run that crashed after publishing, must
        # not publish the shard again.
        stored = next(instance for instance in feed.instances if instance.shard)
        await stored.commit()
        assert feed.publish_count == 1
        assert feed.index_path.read_text() == index
        # Nor must a repeated close of the exporter commit again.
        commit_calls = feed.commit_calls
        await exporter.close_spider(spider)
        assert feed.commit_calls == commit_calls
        assert feed.index_path.read_text() == index

    def test_unsupported_storage_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(unique_path(tmp_path))): {
                    "format": "json",
                    "staged_commit": True,
                },
            },
        }
        crawler = get_crawler(settings_dict=settings)
        with caplog.at_level(logging.ERROR), pytest.raises(NotConfigured):
            build_from_crawler(FeedExporter, crawler)
        assert "does not support staged commit" in caplog.text
