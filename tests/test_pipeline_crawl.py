from __future__ import annotations

import logging
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any

import pytest
from w3lib.url import add_or_replace_parameter

from scrapy import Spider, signals
from scrapy.pipelines.images import ImagesPipeline
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


class DuplicateMediaSpider(SimpleSpider):
    """Yields two items that reference the same media URL but belong to
    different categories, so that a pipeline can route them to different
    paths based on the item."""

    name = "duplicatemedia"

    media_url: str
    media_urls_key: str

    async def start(self):
        for category in ("cat1", "cat2"):
            yield {"category": category, self.media_urls_key: [self.media_url]}


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

    @coroutine_test
    async def test_download_media_shared_by_items(self) -> None:
        """Two items referencing the same media URL share a single download,
        but each item gets its own file_path() result."""
        cls = load_object(self.pipeline_class)

        class CategorizedMediaPipeline(cls):  # type: ignore[misc,valid-type]
            def file_path(self, request, response=None, info=None, *, item=None):
                return f"{item['category']}/file"

        settings = {
            **self.settings,
            "ITEM_PIPELINES": {CategorizedMediaPipeline: 1},
        }
        crawler = self._create_crawler(DuplicateMediaSpider, settings)
        await crawler.crawl_async(
            media_url=self.mockserver.url("/static/files/images/scrapy.png"),
            media_urls_key=self.media_urls_key,
        )

        # the shared media URL was downloaded only once
        assert crawler.stats.get_value("downloader/response_count") == 1

        assert len(self.items) == 2
        results = {}
        for item in self.items:
            file_info = item[self.media_key][0]
            assert file_info["path"] == f"{item['category']}/file"
            assert file_info["status"] == "downloaded"
            results[item["category"]] = file_info
        # each item got its own result object
        assert results["cat1"] is not results["cat2"]
        content1 = (self.tmpmediastore / "cat1" / "file").read_bytes()
        content2 = (self.tmpmediastore / "cat2" / "file").read_bytes()
        assert content1 == content2


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

    @coroutine_test
    async def test_download_media_shared_by_items_thumbnails(self) -> None:
        """When several items share one downloaded image, thumbnail paths
        are also computed per item."""

        class CategorizedImagesPipeline(ImagesPipeline):
            def file_path(self, request, response=None, info=None, *, item=None):
                return f"{item['category']}/full.jpg"

            def thumb_path(
                self, request, thumb_id, response=None, info=None, *, item=None
            ):
                return f"{item['category']}/thumbs/{thumb_id}.jpg"

        settings = {
            **self.settings,
            "ITEM_PIPELINES": {CategorizedImagesPipeline: 1},
            "IMAGES_THUMBS": {"small": (10, 10)},
        }
        crawler = self._create_crawler(DuplicateMediaSpider, settings)
        await crawler.crawl_async(
            media_url=self.mockserver.url("/static/files/images/scrapy.png"),
            media_urls_key=self.media_urls_key,
        )

        # the shared media URL was downloaded only once
        assert crawler.stats.get_value("downloader/response_count") == 1

        assert len(self.items) == 2
        for item in self.items:
            category = item["category"]
            image_info = item[self.media_key][0]
            assert image_info["path"] == f"{category}/full.jpg"
            assert (self.tmpmediastore / category / "full.jpg").exists()
            assert (self.tmpmediastore / category / "thumbs" / "small.jpg").exists()
