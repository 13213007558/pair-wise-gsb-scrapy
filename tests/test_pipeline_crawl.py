from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any

import pytest
from w3lib.url import add_or_replace_parameter

from scrapy import Spider, signals
from scrapy.pipelines.files import FilesPipeline
from scrapy.pipelines.images import ImagesPipeline
from scrapy.pipelines.media import FileException
from scrapy.utils.misc import load_object
from scrapy.utils.test import get_crawler
from tests.mockserver.http import MockServer
from tests.spiders import SimpleSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


class MediaDownloadSpider(SimpleSpider):
    name = "mediadownload"

    media_key: str
    media_urls_key: str

    def _process_url(self, url: str) -> str:
        return url

    def parse(self, response):
        self.logger.info(response.headers)
        self.logger.info(response.text)
        item = {
            self.media_key: [],
            self.media_urls_key: [
                self._process_url(response.urljoin(href))
                for href in response.xpath(
                    '//table[thead/tr/th="Filename"]/tbody//a/@href'
                ).getall()
            ],
        }
        yield item


class BrokenLinksMediaDownloadSpider(MediaDownloadSpider):
    name = "brokenmedia"

    def _process_url(self, url: str) -> str:
        return url + ".foo"


class RedirectedMediaDownloadSpider(MediaDownloadSpider):
    name = "redirectedmedia"

    def _process_url(self, url: str) -> str:
        assert self.mockserver
        return add_or_replace_parameter(
            self.mockserver.url("/redirect-to"), "goto", url
        )


class TestFileDownloadCrawl:
    mockserver: MockServer

    pipeline_class = "scrapy.pipelines.files.FilesPipeline"
    store_setting_key = "FILES_STORE"
    media_key = "files"
    media_urls_key = "file_urls"
    expected_checksums: set[str] | None = {
        "5547178b89448faf0015a13f904c936e",
        "c2281c83670e31d8aaab7cb642b824db",
        "ed3f6538dc15d4d9179dae57319edc5f",
    }

    @classmethod
    def setup_class(cls):
        cls.mockserver = MockServer()
        cls.mockserver.__enter__()

    @classmethod
    def teardown_class(cls):
        cls.mockserver.__exit__(None, None, None)

    def setup_method(self):
        # prepare a directory for storing files
        self.tmpmediastore = Path(mkdtemp())
        self.settings = {
            "ITEM_PIPELINES": {self.pipeline_class: 1},
            self.store_setting_key: str(self.tmpmediastore),
        }
        self.items = []

    def teardown_method(self):
        shutil.rmtree(self.tmpmediastore)
        self.items = []

    def _on_item_scraped(self, item):
        self.items.append(item)

    def _create_crawler(
        self, spider_class: type[Spider], settings: dict[str, Any] | None = None
    ) -> Crawler:
        if settings is None:
            settings = self.settings
        crawler = get_crawler(spider_class, settings)
        crawler.signals.connect(self._on_item_scraped, signals.item_scraped)
        return crawler

    def _assert_files_downloaded(self, items: list[Any], logs: str) -> None:
        assert len(items) == 1
        assert self.media_key in items[0]

        # check that logs show the expected number of successful file downloads
        file_dl_success = "File (downloaded): Downloaded file from"
        assert logs.count(file_dl_success) == 3

        # check that the images/files status is `downloaded`
        for item in items:
            for i in item[self.media_key]:
                assert i["status"] == "downloaded"

        # check that the images/files checksums are what we know they should be
        if self.expected_checksums is not None:
            checksums = {i["checksum"] for item in items for i in item[self.media_key]}
            assert checksums == self.expected_checksums

        # check that the image files where actually written to the media store
        for item in items:
            for i in item[self.media_key]:
                assert (self.tmpmediastore / i["path"]).exists()

    def _assert_files_download_failure(
        self, crawler: Crawler, items: list[Any], code: int, logs: str
    ) -> None:
        # check that the item does NOT have the "images/files" field populated
        assert len(items) == 1
        assert self.media_key in items[0]
        assert not items[0][self.media_key]

        # check that there was 1 successful fetch and 3 other responses with non-200 code
        assert crawler.stats.get_value("downloader/request_method_count/GET") == 4
        assert crawler.stats.get_value("downloader/response_count") == 4
        assert crawler.stats.get_value("downloader/response_status_count/200") == 1
        assert crawler.stats.get_value(f"downloader/response_status_count/{code}") == 3

        # check that logs do show the failure on the file downloads
        file_dl_failure = f"File (code: {code}): Error downloading file from"
        assert logs.count(file_dl_failure) == 3

        # check that no files were written to the media store
        assert not list(self.tmpmediastore.iterdir())

    @coroutine_test
    async def test_download_media(self, caplog: pytest.LogCaptureFixture) -> None:
        crawler = self._create_crawler(MediaDownloadSpider)
        with caplog.at_level(logging.DEBUG):
            await crawler.crawl_async(
                self.mockserver.url("/static/files/images/"),
                media_key=self.media_key,
                media_urls_key=self.media_urls_key,
            )
        self._assert_files_downloaded(self.items, caplog.text)

    @coroutine_test
    async def test_download_media_wrong_urls(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = self._create_crawler(BrokenLinksMediaDownloadSpider)
        with caplog.at_level(logging.DEBUG):
            await crawler.crawl_async(
                self.mockserver.url("/static/files/images/"),
                media_key=self.media_key,
                media_urls_key=self.media_urls_key,
            )
        self._assert_files_download_failure(crawler, self.items, 404, caplog.text)

    @coroutine_test
    async def test_download_media_redirected_default_failure(
        self, caplog: pytest.LogCaptureFixture
    ):
        crawler = self._create_crawler(RedirectedMediaDownloadSpider)
        with caplog.at_level(logging.DEBUG):
            await crawler.crawl_async(
                self.mockserver.url("/static/files/images/"),
                media_key=self.media_key,
                media_urls_key=self.media_urls_key,
                mockserver=self.mockserver,
            )
        self._assert_files_download_failure(crawler, self.items, 302, caplog.text)

    @coroutine_test
    async def test_download_media_redirected_allowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = {
            **self.settings,
            "MEDIA_ALLOW_REDIRECTS": True,
        }
        crawler = self._create_crawler(RedirectedMediaDownloadSpider, settings)
        with caplog.at_level(logging.DEBUG):
            await crawler.crawl_async(
                self.mockserver.url("/static/files/images/"),
                media_key=self.media_key,
                media_urls_key=self.media_urls_key,
                mockserver=self.mockserver,
            )
        self._assert_files_downloaded(self.items, caplog.text)
        assert crawler.stats.get_value("downloader/response_status_count/302") == 3

    @coroutine_test
    async def test_download_media_file_path_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cls = load_object(self.pipeline_class)

        class ExceptionRaisingMediaPipeline(cls):  # type: ignore[misc,valid-type]
            def file_path(self, request, response=None, info=None, *, item=None):
                return 1 / 0

        settings = {
            **self.settings,
            "ITEM_PIPELINES": {ExceptionRaisingMediaPipeline: 1},
        }
        crawler = self._create_crawler(MediaDownloadSpider, settings)
        with caplog.at_level(logging.DEBUG):
            await crawler.crawl_async(
                self.mockserver.url("/static/files/images/"),
                media_key=self.media_key,
                media_urls_key=self.media_urls_key,
                mockserver=self.mockserver,
            )
        assert "ZeroDivisionError" in caplog.text


pillow_available: bool
try:
    from PIL import Image  # noqa: F401
except ImportError:
    pillow_available = False
else:
    pillow_available = True


@pytest.mark.skipif(
    not pillow_available,
    reason="Missing Python Imaging Library, install https://pypi.org/pypi/Pillow",
)
class TestImageDownloadCrawl(TestFileDownloadCrawl):
    pipeline_class = "scrapy.pipelines.images.ImagesPipeline"
    store_setting_key = "IMAGES_STORE"
    media_key = "images"
    media_urls_key = "image_urls"

    # somehow checksums for images are different for Python 3.3
    expected_checksums = None


class TwoItemsMediaSpider(Spider):
    """Yield two items of different categories sharing one media URL."""

    name = "twoitemsmedia"

    media_urls_key: str
    file_url: str

    def parse(self, response):
        for category in ("cat1", "cat2"):
            yield {"category": category, self.media_urls_key: [self.file_url]}


class CategoryFilesPipeline(FilesPipeline):
    def file_path(self, request, response=None, info=None, *, item=None):
        return f"{item['category']}/file"


class FailingCategoryFilesPipeline(CategoryFilesPipeline):
    def file_downloaded(self, response, request, info, *, item=None):
        if item["category"] == "cat2":
            raise FileException("cannot save")
        return super().file_downloaded(response, request, info, item=item)


class CategoryImagesPipeline(ImagesPipeline):
    def file_path(self, request, response=None, info=None, *, item=None):
        return f"{item['category']}/full/image.jpg"

    def thumb_path(self, request, thumb_id, response=None, info=None, *, item=None):
        return f"{item['category']}/thumbs/{thumb_id}/image.jpg"


class FailingCategoryImagesPipeline(CategoryImagesPipeline):
    def file_downloaded(self, response, request, info, *, item=None):
        if item["category"] == "cat2":
            raise FileException("cannot save")
        return super().file_downloaded(response, request, info, item=item)


class TestPerItemMediaCrawl:
    """Items sharing a media URL download it once but each item is processed
    with its own data."""

    mockserver: MockServer
    request_log_dir: Path
    request_log: Path

    pipeline_class: type[FilesPipeline] = CategoryFilesPipeline
    failing_pipeline_class: type[FilesPipeline] = FailingCategoryFilesPipeline
    store_setting_key = "FILES_STORE"
    media_key = "files"
    media_urls_key = "file_urls"
    extra_settings: dict[str, Any] = {}

    @classmethod
    def setup_class(cls):
        cls.request_log_dir = Path(mkdtemp())
        cls.request_log = cls.request_log_dir / "requests.log"
        os.environ["SCRAPY_TEST_REQUEST_LOG"] = str(cls.request_log)
        cls.mockserver = MockServer()
        cls.mockserver.__enter__()

    @classmethod
    def teardown_class(cls):
        cls.mockserver.__exit__(None, None, None)
        del os.environ["SCRAPY_TEST_REQUEST_LOG"]
        shutil.rmtree(cls.request_log_dir)

    def setup_method(self):
        self.request_log.unlink(missing_ok=True)
        self.tmpmediastore = Path(mkdtemp())
        self.settings = {
            "ITEM_PIPELINES": {self.pipeline_class: 1},
            self.store_setting_key: str(self.tmpmediastore),
            **self.extra_settings,
        }
        self.items = []

    def teardown_method(self):
        shutil.rmtree(self.tmpmediastore)

    def _on_item_scraped(self, item):
        self.items.append(item)

    async def _crawl(self, settings: dict[str, Any]) -> None:
        crawler = get_crawler(TwoItemsMediaSpider, settings)
        crawler.signals.connect(self._on_item_scraped, signals.item_scraped)
        await crawler.crawl_async(
            self.mockserver.url("/"),
            media_urls_key=self.media_urls_key,
            file_url=self.mockserver.url("/counting"),
        )

    def _download_count(self) -> int:
        """How many times the media URL was requested from the mock server."""
        if not self.request_log.exists():
            return 0
        return sum(
            line == "/counting" for line in self.request_log.read_text().splitlines()
        )

    def _expected_paths(self, category: str) -> list[str]:
        return [f"{category}/file"]

    def _sorted_items(self) -> list[Any]:
        return sorted(self.items, key=lambda item: item["category"])

    @coroutine_test
    async def test_shared_download_processed_per_item(self):
        await self._crawl(self.settings)
        assert self._download_count() == 1
        item1, item2 = self._sorted_items()
        result1 = item1[self.media_key][0]
        result2 = item2[self.media_key][0]
        assert result1["path"] == self._expected_paths("cat1")[0]
        assert result2["path"] == self._expected_paths("cat2")[0]
        assert result1["status"] == result2["status"] == "downloaded"
        assert result1["checksum"] == result2["checksum"]
        # each item gets its own result object
        assert result1 is not result2
        result1["note"] = "changed"
        assert "note" not in result2
        # each file was saved under the directory of its own item, with the
        # contents of the single shared download
        contents = {}
        for category in ("cat1", "cat2"):
            contents[category] = [
                (self.tmpmediastore / path).read_bytes()
                for path in self._expected_paths(category)
            ]
            assert all(contents[category])
        assert contents["cat1"] == contents["cat2"]

    @coroutine_test
    async def test_per_item_failure_isolation(self):
        settings = {
            **self.settings,
            "ITEM_PIPELINES": {self.failing_pipeline_class: 1},
        }
        await self._crawl(settings)
        assert self._download_count() == 1
        item1, item2 = self._sorted_items()
        assert item1[self.media_key][0]["path"] == self._expected_paths("cat1")[0]
        assert item1[self.media_key][0]["status"] == "downloaded"
        assert item2[self.media_key] == []
        for path in self._expected_paths("cat1"):
            assert (self.tmpmediastore / path).is_file()
        for path in self._expected_paths("cat2"):
            assert not (self.tmpmediastore / path).exists()


@pytest.mark.skipif(
    not pillow_available,
    reason="Missing Python Imaging Library, install https://pypi.org/pypi/Pillow",
)
class TestPerItemImagesCrawl(TestPerItemMediaCrawl):
    pipeline_class = CategoryImagesPipeline
    failing_pipeline_class = FailingCategoryImagesPipeline
    store_setting_key = "IMAGES_STORE"
    media_key = "images"
    media_urls_key = "image_urls"
    extra_settings = {"IMAGES_THUMBS": {"small": (50, 50)}}

    def _expected_paths(self, category: str) -> list[str]:
        return [
            f"{category}/full/image.jpg",
            f"{category}/thumbs/small/image.jpg",
        ]
