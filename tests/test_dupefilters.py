from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from scrapy.core.scheduler import Scheduler
from scrapy.dupefilters import BaseDupeFilter, RFPDupeFilter
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.python import to_bytes
from scrapy.utils.test import get_crawler
from tests.spiders import SimpleSpider

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


def _get_dupefilter(
    *,
    crawler: Crawler | None = None,
    settings: dict[str, Any] | None = None,
    open_: bool = True,
) -> BaseDupeFilter:
    if crawler is None:
        crawler = get_crawler(settings_dict=settings)
    scheduler = build_from_crawler(Scheduler, crawler)
    dupefilter = scheduler.df
    if open_:
        dupefilter.open()
    return dupefilter


class FromCrawlerRFPDupeFilter(RFPDupeFilter):
    method: str

    @classmethod
    def from_crawler(cls, crawler):
        df = super().from_crawler(crawler)
        df.method = "from_crawler"
        return df


class DirectDupeFilter:
    method = "n/a"


class TestRFPDupeFilter:
    def test_df_from_crawler_scheduler(self):
        settings = {
            "DUPEFILTER_DEBUG": True,
            "DUPEFILTER_CLASS": FromCrawlerRFPDupeFilter,
        }
        crawler = get_crawler(settings_dict=settings)
        scheduler = build_from_crawler(Scheduler, crawler)
        dupefilter = cast("FromCrawlerRFPDupeFilter", scheduler.df)
        assert dupefilter.debug
        assert dupefilter.method == "from_crawler"

    def test_df_direct_scheduler(self):
        settings = {
            "DUPEFILTER_CLASS": DirectDupeFilter,
        }
        crawler = get_crawler(settings_dict=settings)
        scheduler = build_from_crawler(Scheduler, crawler)
        assert cast("DirectDupeFilter", scheduler.df).method == "n/a"

    def test_filter(self):
        dupefilter = _get_dupefilter()
        r1 = Request("http://scrapytest.org/1")
        r2 = Request("http://scrapytest.org/2")
        r3 = Request("http://scrapytest.org/2")

        assert not dupefilter.request_seen(r1)
        assert dupefilter.request_seen(r1)

        assert not dupefilter.request_seen(r2)
        assert dupefilter.request_seen(r3)

        dupefilter.close("finished")

    @pytest.mark.parametrize(
        ("first_depth", "second_depth", "second_seen"),
        [(2, 2, True), (2, 3, True), (2, 1, False), (1, 0, False)],
    )
    def test_shortest_depth(
        self, first_depth: int, second_depth: int, second_seen: bool
    ) -> None:
        dupefilter = cast(
            RFPDupeFilter,
            _get_dupefilter(settings={"DUPEFILTER_SHORTEST_DEPTH": True}),
        )
        request = Request("https://example.com")

        request.meta["depth"] = first_depth
        assert not dupefilter.request_seen(request)

        request.meta["depth"] = second_depth
        assert dupefilter.request_seen(request) is second_seen

        dupefilter.close("finished")

    def test_shortest_depth_missing_depth(self) -> None:
        dupefilter = cast(
            RFPDupeFilter,
            _get_dupefilter(settings={"DUPEFILTER_SHORTEST_DEPTH": True}),
        )

        assert not dupefilter.request_seen(
            Request("https://example.com", meta={"depth": 1})
        )
        assert not dupefilter.request_seen(Request("https://example.com"))
        assert dupefilter.request_seen(Request("https://example.com"))

        dupefilter.close("finished")

    def test_shortest_depth_dont_filter_does_not_update_depth(self) -> None:
        crawler = get_crawler(
            SimpleSpider,
            {
                "DUPEFILTER_SHORTEST_DEPTH": True,
                "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.ScrapyPriorityQueue",
            },
        )
        scheduler = build_from_crawler(Scheduler, crawler)
        spider = SimpleSpider.from_crawler(crawler)
        scheduler.open(spider)
        request = Request("https://example.com", meta={"depth": 1})

        assert scheduler.enqueue_request(request)
        request.dont_filter = True
        request.meta["depth"] = 0
        assert scheduler.enqueue_request(request)
        request.dont_filter = False
        assert scheduler.enqueue_request(request)

        scheduler.close("finished")

    def test_shortest_depth_persistence(self, tmp_path: Path) -> None:
        request = Request("https://example.com")
        df = cast(
            RFPDupeFilter,
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            ),
        )
        df.open()
        request.meta["depth"] = 2
        assert not df.request_seen(request)
        df.close("finished")

        df2 = cast(
            RFPDupeFilter,
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            ),
        )
        df2.open()
        assert df2.request_seen(
            Request("https://example.com", meta={"depth": 2})
        )
        assert not df2.request_seen(
            Request("https://example.com", meta={"depth": 1})
        )
        df2.close("finished")

    def test_shortest_depth_rejects_legacy_job_dir(self, tmp_path: Path) -> None:
        legacy_df = _get_dupefilter(
            settings={"JOBDIR": str(tmp_path)},
            open_=False,
        )
        legacy_df.open()
        legacy_df.request_seen(Request("https://example.com"))
        legacy_df.close("finished")

        with pytest.raises(ValueError, match="created without it"):
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            )

    def test_default_dupefilter_rejects_shortest_depth_job_dir(
        self, tmp_path: Path
    ) -> None:
        depth_df = cast(
            RFPDupeFilter,
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            ),
        )
        depth_df.open()
        depth_df.close("finished")

        with pytest.raises(ValueError, match="created with"):
            _get_dupefilter(
                settings={"JOBDIR": str(tmp_path)},
                open_=False,
            )

    def test_shortest_depth_rejects_unknown_format(self, tmp_path: Path) -> None:
        (tmp_path / "requests.depth.seen").write_bytes(b"unsupported")

        with pytest.raises(ValueError, match="Unsupported request depth file"):
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            )

    def test_shortest_depth_rejects_truncated_format(self, tmp_path: Path) -> None:
        depth_df = cast(
            RFPDupeFilter,
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            ),
        )
        depth_df.open()
        depth_df.close("finished")
        depth_file = tmp_path / "requests.depth.seen"
        depth_file.write_bytes(depth_file.read_bytes() + b"\x00")

        with pytest.raises(ValueError, match="Truncated request depth file"):
            _get_dupefilter(
                settings={
                    "DUPEFILTER_SHORTEST_DEPTH": True,
                    "JOBDIR": str(tmp_path),
                },
                open_=False,
            )

    def test_dupefilter_path(self):
        r1 = Request("http://scrapytest.org/1")
        r2 = Request("http://scrapytest.org/2")

        path = tempfile.mkdtemp()
        try:
            df = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            try:
                df.open()
                assert not df.request_seen(r1)
                assert df.request_seen(r1)
            finally:
                df.close("finished")

            df2 = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            assert df is not df2
            try:
                df2.open()
                assert df2.request_seen(r1)
                assert not df2.request_seen(r2)
                assert df2.request_seen(r2)
            finally:
                df2.close("finished")
        finally:
            shutil.rmtree(path)

    def test_request_fingerprint(self):
        """Test if customization of request_fingerprint method will change
        output of request_seen.

        """
        dupefilter = _get_dupefilter()
        r1 = Request("http://scrapytest.org/index.html")
        r2 = Request("http://scrapytest.org/INDEX.html")

        assert not dupefilter.request_seen(r1)
        assert not dupefilter.request_seen(r2)

        dupefilter.close("finished")

        class RequestFingerprinter:
            def fingerprint(self, request):
                fp = hashlib.sha1()
                fp.update(to_bytes(request.url.lower()))
                return fp.digest()

        settings = {"REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter}
        case_insensitive_dupefilter = _get_dupefilter(settings=settings)

        assert not case_insensitive_dupefilter.request_seen(r1)
        assert case_insensitive_dupefilter.request_seen(r2)

        case_insensitive_dupefilter.close("finished")

    def test_seenreq_truncated(self):
        r1 = Request("http://scrapytest.org/1")
        r2 = Request("http://scrapytest.org/2")

        path = tempfile.mkdtemp()
        try:
            df = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            try:
                df.open()
                df.request_seen(r1)
                df.request_seen(r2)
            finally:
                df.close("finished")

            seen_file = Path(path, "requests.seen")
            seen_file.write_bytes(seen_file.read_bytes()[:-1])

            df2 = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            try:
                df2.open()
                assert df2.request_seen(r1)
                assert not df2.request_seen(r2)
            finally:
                df2.close("finished")
        finally:
            shutil.rmtree(path)

    def test_log(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = {
            "DUPEFILTER_DEBUG": False,
            "DUPEFILTER_CLASS": FromCrawlerRFPDupeFilter,
        }
        crawler = get_crawler(SimpleSpider, settings_dict=settings)
        spider = SimpleSpider.from_crawler(crawler)
        dupefilter = _get_dupefilter(crawler=crawler)

        r1 = Request("http://scrapytest.org/index.html")
        r2 = Request("http://scrapytest.org/index.html")

        with caplog.at_level(logging.DEBUG):
            dupefilter.log(r1, spider)
            dupefilter.log(r2, spider)

        assert crawler.stats.get_value("dupefilter/filtered") == 2
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            (
                "Filtered duplicate request: <GET http://scrapytest.org/index.html> - no more"
                " duplicates will be shown (see DUPEFILTER_DEBUG to show all duplicates)"
            ),
        ) in caplog.record_tuples

        dupefilter.close("finished")

    @pytest.mark.parametrize("df", [None, FromCrawlerRFPDupeFilter])
    def test_log_debug(
        self, caplog: pytest.LogCaptureFixture, df: type[BaseDupeFilter] | None
    ) -> None:
        settings: dict[str, Any] = {
            "DUPEFILTER_DEBUG": True,
        }
        if df:
            settings["DUPEFILTER_CLASS"] = df
        crawler = get_crawler(SimpleSpider, settings_dict=settings)
        spider = SimpleSpider.from_crawler(crawler)
        dupefilter = _get_dupefilter(crawler=crawler)

        r1 = Request("http://scrapytest.org/index.html")
        r2 = Request(
            "http://scrapytest.org/index.html",
            headers={"Referer": "http://scrapytest.org/INDEX.html"},
        )

        with caplog.at_level(logging.DEBUG):
            dupefilter.log(r1, spider)
            dupefilter.log(r2, spider)

        assert crawler.stats.get_value("dupefilter/filtered") == 2
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            "Filtered duplicate request: <GET http://scrapytest.org/index.html> (referer: None)",
        ) in caplog.record_tuples
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            (
                "Filtered duplicate request: <GET http://scrapytest.org/index.html>"
                " (referer: http://scrapytest.org/INDEX.html)"
            ),
        ) in caplog.record_tuples

        dupefilter.close("finished")

    def test_fingerprints_deprecation(self):
        dupefilter = cast("RFPDupeFilter", _get_dupefilter())
        request = Request("http://scrapytest.org/index.html")
        dupefilter.request_seen(request)
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"RFPDupeFilter\.fingerprints is deprecated\.",
        ):
            fingerprints = dupefilter.fingerprints
        assert fingerprints == {dupefilter.request_fingerprint(request)}
        dupefilter.close("finished")

    def test_request_fingerprint_override_deprecation(self):
        class LegacyDupeFilter(RFPDupeFilter):
            def request_fingerprint(self, request):
                return hashlib.sha1(to_bytes(request.url.lower())).hexdigest()

        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"Overriding RFPDupeFilter\.request_fingerprint\(\) is deprecated",
        ):
            dupefilter = _get_dupefilter(
                settings={"DUPEFILTER_CLASS": LegacyDupeFilter}
            )

        assert not dupefilter.request_seen(Request("http://scrapytest.org/index.html"))
        assert dupefilter.request_seen(Request("http://scrapytest.org/INDEX.html"))
        dupefilter.close("finished")


class TestBaseDupeFilter:
    def test_log_deprecation(self):
        dupefilter = _get_dupefilter(
            settings={"DUPEFILTER_CLASS": BaseDupeFilter},
        )
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"Calling BaseDupeFilter\.log\(\) is deprecated.",
        ):
            dupefilter.log(None, None)  # type: ignore[arg-type]
