from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from scrapy.core.scheduler import Scheduler
from scrapy.dupefilters import BaseDupeFilter, RFPDupeFilter
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.python import to_bytes
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider, SimpleSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


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

    def test_scoped_filtering(self):
        dupefilter = _get_dupefilter(
            settings={
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": [
                    "utm_medium",
                    "utm_source",
                ],
                "REQUEST_FINGERPRINTER_SCOPE_META_KEY": "identity_scope",
            }
        )
        r1 = Request(
            "http://scrapytest.org/?id=1&utm_source=first",
            meta={"identity_scope": "account-a"},
        )
        r2 = Request(
            "http://scrapytest.org/?utm_medium=email&id=1&utm_source=second",
            meta={"identity_scope": "account-a"},
        )
        r3 = Request(
            "http://scrapytest.org/?id=1&utm_source=first",
            meta={"identity_scope": "account-b"},
        )

        assert not dupefilter.request_seen(r1)
        assert dupefilter.request_seen(r2)
        assert not dupefilter.request_seen(r3)

        dupefilter.close("finished")

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

    def test_fingerprint_policy_file(self, tmp_path: Path) -> None:
        settings = {
            "JOBDIR": str(tmp_path),
            "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["utm_source"],
            "REQUEST_FINGERPRINTER_SCOPE_META_KEY": "identity_scope",
        }
        request = Request(
            "http://scrapytest.org/?id=1&utm_source=first",
            meta={"identity_scope": "account-a"},
        )
        dupefilter = _get_dupefilter(settings=settings)
        try:
            assert not dupefilter.request_seen(request)
        finally:
            dupefilter.close("finished")

        policy_path = tmp_path / "requests.seen.policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        assert policy["version"] == 1
        assert policy["ignore_query_params"] == ["utm_source"]
        assert policy["scope_meta_key"] == "identity_scope"
        assert "account-a" not in json.dumps(policy)

        resumed = _get_dupefilter(settings=settings)
        try:
            assert resumed.request_seen(request)
        finally:
            resumed.close("finished")

    def test_fingerprint_policy_change_is_rejected(self, tmp_path: Path) -> None:
        settings = {
            "JOBDIR": str(tmp_path),
            "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["utm_source"],
        }
        dupefilter = _get_dupefilter(settings=settings)
        dupefilter.close("finished")
        seen_before = (tmp_path / "requests.seen").read_bytes()

        changed_settings = {
            **settings,
            "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["utm_medium"],
        }
        with pytest.raises(ValueError, match=r"policy\.ignore_query_params") as excinfo:
            _get_dupefilter(settings=changed_settings)

        assert "['utm_source'] != ['utm_medium']" in str(excinfo.value)
        assert (tmp_path / "requests.seen").read_bytes() == seen_before

    def test_legacy_fingerprint_policy_change_is_rejected(self, tmp_path: Path) -> None:
        dupefilter = _get_dupefilter(settings={"JOBDIR": str(tmp_path)})
        dupefilter.request_seen(Request("http://scrapytest.org/?id=1"))
        dupefilter.close("finished")
        (tmp_path / "requests.seen.policy.json").unlink()
        seen_before = (tmp_path / "requests.seen").read_bytes()

        with pytest.raises(ValueError, match="legacy default policy") as excinfo:
            _get_dupefilter(
                settings={
                    "JOBDIR": str(tmp_path),
                    "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["utm_source"],
                }
            )

        assert "policy.ignore_query_params" in str(excinfo.value)
        assert (tmp_path / "requests.seen").read_bytes() == seen_before

        resumed = _get_dupefilter(settings={"JOBDIR": str(tmp_path)})
        resumed.close("finished")
        assert (tmp_path / "requests.seen.policy.json").exists()

    @pytest.mark.parametrize(
        ("policy_change", "difference"),
        [
            ({"remove": "version"}, "policy.version"),
            ({"add": ("extra", 1)}, "policy.extra"),
        ],
    )
    def test_fingerprint_policy_file_shape_change_is_rejected(
        self,
        tmp_path: Path,
        policy_change: dict[str, Any],
        difference: str,
    ) -> None:
        settings = {"JOBDIR": str(tmp_path)}
        dupefilter = _get_dupefilter(settings=settings)
        dupefilter.close("finished")
        policy_path = tmp_path / "requests.seen.policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if "remove" in policy_change:
            policy.pop(policy_change["remove"])
        else:
            key, value = policy_change["add"]
            policy[key] = value
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        with pytest.raises(ValueError, match=difference):
            _get_dupefilter(settings=settings)

    def test_invalid_fingerprint_policy_file_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "requests.seen.policy.json").write_text(
            "not JSON", encoding="utf-8"
        )

        with pytest.raises(ValueError, match="cannot read the request fingerprint"):
            _get_dupefilter(settings={"JOBDIR": str(tmp_path)})

    def test_custom_fingerprinter_without_policy(self, tmp_path: Path) -> None:
        class RequestFingerprinter:
            def fingerprint(self, request: Request) -> bytes:
                return hashlib.sha1(to_bytes(request.url)).digest()

        settings = {
            "JOBDIR": str(tmp_path),
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        dupefilter = _get_dupefilter(settings=settings)
        dupefilter.close("finished")
        assert not (tmp_path / "requests.seen.policy.json").exists()

        default_dupefilter = _get_dupefilter(settings={"JOBDIR": str(tmp_path)})
        default_dupefilter.close("finished")

        with pytest.raises(ValueError, match="does not provide a fingerprint policy"):
            _get_dupefilter(settings=settings)

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

    @coroutine_test
    async def test_scoped_crawl_preserves_urls_and_jobdir_secrets(
        self, tmp_path: Path, mockserver: MockServer
    ) -> None:
        class ScopedSpider(MockServerSpider):
            name = "scoped-identity"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.downloaded: list[tuple[str, str, str]] = []

            async def start(self) -> AsyncIterator[Request]:
                assert self.mockserver
                first = self.mockserver.url("/uri?id=1&id=2&utm_source=alpha&empty=")
                duplicate = self.mockserver.url(
                    "/uri?utm_medium=email&id=2&empty=&id=1&utm_source=beta"
                )
                for account, url in (
                    ("account-a", first),
                    ("account-a", duplicate),
                    ("account-b", first),
                ):
                    yield Request(
                        url,
                        headers={
                            "Authorization": f"Bearer {account}-secret",
                            "X-Request-Kind": "identity-test",
                        },
                        meta={"identity_scope": account},
                    )

            def parse(self, response: Response) -> None:
                assert response.request is not None
                self.downloaded.append(
                    (
                        response.request.meta["identity_scope"],
                        response.request.url,
                        response.body.decode(),
                    )
                )

        crawler = get_crawler(
            ScopedSpider,
            settings_dict={
                "JOBDIR": str(tmp_path),
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": [
                    "utm_medium",
                    "utm_source",
                ],
                "REQUEST_FINGERPRINTER_INCLUDE_HEADERS": ["X-Request-Kind"],
                "REQUEST_FINGERPRINTER_SCOPE_META_KEY": "identity_scope",
            },
        )

        await crawler.crawl_async(mockserver=mockserver)

        assert crawler.stats.get_value("scheduler/enqueued") == 2
        assert crawler.stats.get_value("dupefilter/filtered") == 1
        spider = cast("ScopedSpider", crawler.spider)
        assert [account for account, _, _ in spider.downloaded] == [
            "account-a",
            "account-b",
        ]
        for _, url, body in spider.downloaded:
            assert "utm_source=alpha" in url
            assert "id=1&id=2" in url
            assert "empty=" in url
            assert "utm_source=alpha" in body

        policy_path = tmp_path / "requests.seen.policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        assert policy["version"] == 1
        assert policy["ignore_query_params"] == ["utm_medium", "utm_source"]
        assert policy["include_headers"] == ["x-request-kind"]
        assert policy["scope_meta_key"] == "identity_scope"
        assert "account-a" not in json.dumps(policy)
        assert "account-b" not in json.dumps(policy)
        assert len((tmp_path / "requests.seen").read_bytes()) == 2 * (2 + 20)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                assert b"account-a-secret" not in contents
                assert b"account-b-secret" not in contents


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
