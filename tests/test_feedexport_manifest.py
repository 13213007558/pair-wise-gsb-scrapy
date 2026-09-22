from __future__ import annotations

import gzip
import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest

from scrapy import Spider
from scrapy.exceptions import NotConfigured
from scrapy.extensions.feedexport import FeedExporter, FileFeedStorage
from scrapy.extensions.postprocessing import GzipPlugin
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler, get_testenv
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from os import PathLike

    from twisted.internet.defer import Deferred

    from scrapy.crawler import Crawler


def _spider_cls(items: list[dict[str, Any]]) -> type[Spider]:
    class TestSpider(Spider):
        name = "testspider"

        async def start(self) -> AsyncIterator[dict[str, Any]]:
            for item in items:
                yield item

    return TestSpider


async def _crawl(settings: dict[str, Any], items: list[dict[str, Any]]) -> Crawler:
    crawler = get_crawler(_spider_cls(items), settings)
    await crawler.crawl_async()
    return crawler


def _feed_settings(
    feed_uri: str | PathLike[str], manifest: Path, **feed_options: Any
) -> dict[str, Any]:
    return {
        "FEEDS": {
            str(feed_uri): {"format": "jl", "manifest": str(manifest), **feed_options}
        }
    }


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data


def _assert_committed_file(entry: dict[str, Any]) -> Path:
    file = Path(entry["file"])
    assert file.is_file()
    assert entry["bytes"] == file.stat().st_size
    assert entry["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    return file


class TestManifestCommits:
    @coroutine_test
    async def test_committed_batches(self, tmp_path: Path) -> None:
        feed_uri = str(tmp_path / "items-%(batch_id)d.jl.gz")
        manifest_path = tmp_path / "subdir" / "items.manifest.json"
        settings = _feed_settings(
            feed_uri,
            manifest_path,
            batch_item_count=2,
            postprocessing=[GzipPlugin],
            custom_option=Path("custom-value"),
        )
        await _crawl(settings, [{"n": i} for i in range(5)])

        data = _read_manifest(manifest_path)
        assert data["version"] == 1
        assert data["feed"] == feed_uri
        batches = data["batches"]
        assert [entry["batch_id"] for entry in batches] == [1, 2, 3]
        assert [entry["itemcount"] for entry in batches] == [2, 2, 1]
        items: list[dict[str, Any]] = []
        for entry in batches:
            file = _assert_committed_file(entry)
            with gzip.open(file, "rt", encoding="utf-8") as f:
                items.extend(json.loads(line) for line in f)
        assert items == [{"n": i} for i in range(5)]

    @coroutine_test
    async def test_multiple_feeds(self, tmp_path: Path) -> None:
        feeds = {
            str(tmp_path / f"{name}-%(batch_id)d.jl"): {
                "format": "jl",
                "batch_item_count": 1,
                "manifest": str(tmp_path / f"{name}.manifest.json"),
            }
            for name in ("a", "b")
        }
        await _crawl({"FEEDS": feeds}, [{"n": 0}, {"n": 1}])

        for name in ("a", "b"):
            batches = _read_manifest(tmp_path / f"{name}.manifest.json")["batches"]
            assert [entry["batch_id"] for entry in batches] == [1, 2]
            for entry in batches:
                file = _assert_committed_file(entry)
                assert file.name.startswith(f"{name}-")

    @coroutine_test
    async def test_failed_batch_is_not_committed(self, tmp_path: Path) -> None:
        class FailingFileStorage(FileFeedStorage):
            def store(self, file: IO[bytes]) -> Deferred[None] | None:
                if self.path.endswith("-2.jl"):
                    file.close()
                    raise OSError("simulated storage failure")
                return super().store(file)

        feed_uri = str(tmp_path / "items-%(batch_id)d.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path, batch_item_count=1)
        settings["FEED_STORAGES"] = {"": FailingFileStorage}
        crawler = await _crawl(settings, [{"n": i} for i in range(3)])

        assert (
            crawler.stats.get_value("feedexport/failed_count/FailingFileStorage") == 1
        )
        batches = _read_manifest(manifest_path)["batches"]
        assert [entry["batch_id"] for entry in batches] == [1, 3]
        # The file of the failed batch stays on disk, but downstream
        # consumers reading only the manifest never see it.
        assert (tmp_path / "items-2.jl").is_file()
        for entry in batches:
            _assert_committed_file(entry)

    @coroutine_test
    async def test_unwritable_manifest_does_not_fail_crawl(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        feed_uri = str(tmp_path / "items.jl")
        settings = _feed_settings(feed_uri, blocker / "items.manifest.json")
        with caplog.at_level(logging.ERROR):
            crawler = await _crawl(settings, [{"n": 0}])

        assert "Error committing" in caplog.text
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 1
        assert (tmp_path / "items.jl").is_file()


class TestManifestResume:
    @coroutine_test
    async def test_resume_continues_after_last_committed_batch(
        self, tmp_path: Path
    ) -> None:
        feed_uri = str(tmp_path / "items-%(batch_id)d.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path, batch_item_count=1)
        await _crawl(settings, [{"n": i} for i in range(3)])
        committed = _read_manifest(manifest_path)["batches"]
        assert [entry["batch_id"] for entry in committed] == [1, 2, 3]

        await _crawl(settings, [{"n": i} for i in range(3, 5)])

        batches = _read_manifest(manifest_path)["batches"]
        assert [entry["batch_id"] for entry in batches] == [1, 2, 3, 4, 5]
        assert batches[:3] == committed
        for entry in batches:
            _assert_committed_file(entry)

    @coroutine_test
    async def test_resume_truncates_uncommitted_leftover(
        self, tmp_path: Path
    ) -> None:
        feed_uri = str(tmp_path / "items-%(batch_id)d.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path, batch_item_count=1)
        await _crawl(settings, [{"n": 0}, {"n": 1}])
        leftover = tmp_path / "items-3.jl"
        leftover.write_bytes(b'{"n": "partial')

        await _crawl(settings, [{"n": 2}])

        batches = _read_manifest(manifest_path)["batches"]
        assert [entry["batch_id"] for entry in batches] == [1, 2, 3]
        assert leftover.read_bytes() == b'{"n": 2}\n'
        _assert_committed_file(batches[2])

    @coroutine_test
    async def test_resume_does_not_overwrite_committed_file(
        self, tmp_path: Path
    ) -> None:
        feed_uri = str(tmp_path / "items.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path)
        await _crawl(settings, [{"n": 0}])
        file = tmp_path / "items.jl"
        content = file.read_bytes()

        crawler = await _crawl(settings, [{"n": 1}])

        assert "would overwrite" in crawler.stats.get_value("finish_reason", "")
        assert file.read_bytes() == content
        assert len(_read_manifest(manifest_path)["batches"]) == 1

    @pytest.mark.parametrize(
        ("corrupt", "message"),
        [
            ("missing", "of batch 2 is missing"),
            ("grow", "of batch 2 has a different size"),
            ("modify", "of batch 2 does not match its recorded SHA-256"),
        ],
    )
    @coroutine_test
    async def test_resume_with_mismatched_committed_file(
        self, tmp_path: Path, corrupt: str, message: str
    ) -> None:
        feed_uri = str(tmp_path / "items-%(batch_id)d.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path, batch_item_count=1)
        await _crawl(settings, [{"n": i} for i in range(3)])
        file = tmp_path / "items-2.jl"
        if corrupt == "missing":
            file.unlink()
        elif corrupt == "grow":
            file.write_bytes(file.read_bytes() + b"x")
        else:
            content = bytearray(file.read_bytes())
            content[0] ^= 0xFF
            file.write_bytes(bytes(content))

        with pytest.raises(ValueError, match=message):
            get_crawler(_spider_cls([]), settings)

    @coroutine_test
    async def test_resume_with_changed_feed_options(self, tmp_path: Path) -> None:
        feed_uri = str(tmp_path / "items-%(batch_id)d.jl")
        manifest_path = tmp_path / "items.manifest.json"
        settings = _feed_settings(feed_uri, manifest_path, batch_item_count=1)
        await _crawl(settings, [{"n": 0}])

        settings["FEEDS"][feed_uri]["indent"] = 2
        with pytest.raises(ValueError, match="different feed options"):
            get_crawler(_spider_cls([]), settings)


class TestManifestConfiguration:
    def test_manifest_requires_file_storage(self, tmp_path: Path) -> None:
        settings: dict[str, Any] = {
            "FEEDS": {
                "stdout:": {
                    "format": "jl",
                    "manifest": str(tmp_path / "items.manifest.json"),
                }
            }
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured, match="only supported with local file"):
            build_from_crawler(FeedExporter, crawler)

    def test_feeds_cannot_share_a_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "items.manifest.json"
        settings: dict[str, Any] = {
            "FEEDS": {
                str(tmp_path / f"{name}-%(batch_id)d.jl"): {
                    "format": "jl",
                    "batch_item_count": 1,
                    "manifest": str(manifest_path),
                }
                for name in ("a", "b")
            }
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured, match="cannot share a manifest"):
            build_from_crawler(FeedExporter, crawler)

    def test_unparseable_manifest(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "items.manifest.json"
        manifest_path.write_text("not json", encoding="utf-8")
        settings = _feed_settings(tmp_path / "items.jl", manifest_path)
        with pytest.raises(ValueError, match="Cannot parse feed manifest"):
            get_crawler(_spider_cls([]), settings)

    def test_unreadable_manifest(self, tmp_path: Path) -> None:
        settings = _feed_settings(tmp_path / "items.jl", tmp_path)
        with pytest.raises(ValueError, match="Cannot parse feed manifest"):
            get_crawler(_spider_cls([]), settings)

    @pytest.mark.parametrize(
        "content",
        [
            '{"version": 2, "batches": []}',
            '{"version": 1}',
        ],
    )
    def test_unsupported_manifest_format(
        self, tmp_path: Path, content: str
    ) -> None:
        manifest_path = tmp_path / "items.manifest.json"
        manifest_path.write_text(content, encoding="utf-8")
        settings = _feed_settings(tmp_path / "items.jl", manifest_path)
        with pytest.raises(ValueError, match="Unsupported feed manifest format"):
            get_crawler(_spider_cls([]), settings)


_CRAWL_SCRIPT = """
import os
import sys
import time
from pathlib import Path

from scrapy import Spider
from scrapy.crawler import CrawlerProcess
from scrapy.extensions.feedexport import FileFeedStorage


class BlockingFileStorage(FileFeedStorage):
    def store(self, file):
        super().store(file)
        if os.environ.get("BLOCK_STORE"):
            marker_dir = Path(os.environ["MARKER_DIR"])
            (marker_dir / "store-entered").touch()
            deadline = time.monotonic() + 60
            while not (marker_dir / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.05)


class ItemSpider(Spider):
    name = "item-spider"

    async def start(self):
        for n in range(5):
            yield {"n": n}


out_dir = Path(sys.argv[1])
process = CrawlerProcess(
    {
        "FEEDS": {
            str(out_dir / "items-%(batch_id)d.jl.gz"): {
                "format": "jl",
                "batch_item_count": 1,
                "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                "manifest": str(out_dir / "items.manifest.json"),
            }
        },
        "FEED_STORAGES": {"": "__main__.BlockingFileStorage"},
    }
)
process.crawl(ItemSpider)
process.start()
"""


class TestKillAndResume:
    @staticmethod
    def _wait_for(path: Path, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.05)
        raise TimeoutError(f"Timed out waiting for {path}")

    def test_kill_between_store_and_commit(self, tmp_path: Path) -> None:
        script = tmp_path / "crawl_to_manifest.py"
        script.write_text(_CRAWL_SCRIPT, encoding="utf-8")
        out_dir = tmp_path / "out"
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()
        env = get_testenv()
        env["MARKER_DIR"] = str(marker_dir)
        env["BLOCK_STORE"] = "1"

        # Kill the run after the first batch file is fully written and
        # post-processed but before it is committed to the manifest.
        with subprocess.Popen(
            [sys.executable, str(script), str(out_dir)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            try:
                self._wait_for(marker_dir / "store-entered")
            finally:
                proc.kill()
            proc.wait()

        manifest_path = out_dir / "items.manifest.json"
        assert (out_dir / "items-1.jl.gz").is_file()
        if manifest_path.exists():
            assert _read_manifest(manifest_path)["batches"] == []

        # Resume: uncommitted leftovers are rewritten, and every manifest
        # entry must match its final compressed file.
        del env["BLOCK_STORE"]
        result = subprocess.run(
            [sys.executable, str(script), str(out_dir)],
            env=env,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()

        batches = _read_manifest(manifest_path)["batches"]
        assert [entry["batch_id"] for entry in batches] == [1, 2, 3, 4, 5]
        items: list[dict[str, Any]] = []
        for entry in batches:
            file = _assert_committed_file(entry)
            with gzip.open(file, "rt", encoding="utf-8") as f:
                items.extend(json.loads(line) for line in f)
        assert items == [{"n": n} for n in range(5)]
