from __future__ import annotations

import email.utils
import logging
import pickle
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from scrapy.downloadermiddlewares.httpcache import HttpCacheMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.extensions.httpcache import DummyPolicy, rfc1123_to_epoch
from scrapy.http import HtmlResponse, Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator

    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


class CustomResponse(Response):
    attributes: tuple[str, ...] = (*Response.attributes, "custom")

    def __init__(self, *args: Any, custom: str | None = None, **kwargs: Any):
        self.custom = custom
        super().__init__(*args, **kwargs)


class AlwaysStalePolicy(DummyPolicy):
    """:class:`~scrapy.extensions.httpcache.DummyPolicy` that always
    revalidates cached responses."""

    def is_cached_response_fresh(self, cachedresponse, request):
        return False


class TestBase:
    """Base class with common setup and helper methods."""

    policy_class: str
    storage_class: str

    def setup_method(self):
        self.yesterday = email.utils.formatdate(time.time() - 86400)
        self.today = email.utils.formatdate()
        self.tomorrow = email.utils.formatdate(time.time() + 86400)
        self.tmpdir = tempfile.mkdtemp()
        self.request = Request("http://www.example.com", headers={"User-Agent": "test"})
        self.response = Response(
            "http://www.example.com",
            headers={"Content-Type": "text/html"},
            body=b"test body",
            status=202,
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _get_settings(self, **new_settings: Any) -> dict[str, Any]:
        settings = {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": self.tmpdir,
            "HTTPCACHE_IGNORE_HTTP_CODES": [],
            "HTTPCACHE_POLICY": self.policy_class,
            "HTTPCACHE_STORAGE": self.storage_class,
        }
        settings.update(new_settings)
        return settings

    @contextmanager
    def _get_crawler(self, **new_settings: Any) -> Generator[Crawler]:
        settings = self._get_settings(**new_settings)
        crawler = get_crawler(Spider, settings)
        crawler.spider = crawler._create_spider("example.com")
        crawler.stats.open_spider()
        try:
            yield crawler
        finally:
            crawler.stats.close_spider()

    @contextmanager
    def _storage(self, **new_settings: Any):
        with self._middleware(**new_settings) as mw:
            yield mw.storage, mw.crawler

    @contextmanager
    def _middleware(self, **new_settings: Any) -> Generator[HttpCacheMiddleware]:
        with self._get_crawler(**new_settings) as crawler:
            assert crawler.spider
            mw = build_from_crawler(HttpCacheMiddleware, crawler)
            mw.spider_opened(crawler.spider)
            try:
                yield mw
            finally:
                mw.spider_closed(crawler.spider)

    def assertEqualResponse(self, response1: Response, response2: Response) -> None:
        assert response1.url == response2.url
        assert response1.status == response2.status
        assert response1.headers == response2.headers
        assert response1.body == response2.body


class StorageTestMixin(TestBase):
    """Mixin containing storage-specific test methods."""

    def _corrupt_cache_entry(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Make the cache entry of *request* unreadable for *storage*."""
        raise NotImplementedError

    def _downgrade_cache_entry(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Rewrite the cache entry of *request* as Scrapy 2.14 would have."""
        raise NotImplementedError

    def _downgrade_vary_cache_entry(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Rewrite the cache entry of *request*, which must hold a single
        variant, as Scrapy 2.14 would have (no variant metadata)."""
        raise NotImplementedError

    def _delete_vary_variant_metadata(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Delete the metadata of the stored variant of *request*, which
        must hold a single variant."""
        raise NotImplementedError

    def test_storage(self):
        with self._storage(HTTPCACHE_EXPIRATION_SECS=100) as (storage, crawler):
            request2 = self.request.copy()
            assert storage.retrieve_response(crawler.spider, request2) is None

            before = time.time()
            storage.store_response(crawler.spider, self.request, self.response)
            after = time.time()
            response2 = storage.retrieve_response(crawler.spider, request2)
            assert isinstance(response2, HtmlResponse)  # content-type header
            self.assertEqualResponse(self.response, response2)
            assert before <= request2.meta["cache_timestamp"] <= after

            expired = time.time() + storage.expiration_secs + 1
            with mock.patch("scrapy.extensions.httpcache.time", return_value=expired):
                assert storage.retrieve_response(crawler.spider, request2) is None

    def test_storage_never_expire(self):
        with self._storage(HTTPCACHE_EXPIRATION_SECS=0) as (storage, crawler):
            assert storage.retrieve_response(crawler.spider, self.request) is None
            storage.store_response(crawler.spider, self.request, self.response)
            future = time.time() + 10**6
            with mock.patch("scrapy.extensions.httpcache.time", return_value=future):
                assert storage.retrieve_response(crawler.spider, self.request)

    def test_corrupted_cache_entry_is_a_miss(self, caplog):
        with self._middleware() as mw:
            spider = mw.crawler.spider
            assert spider
            mw.storage.store_response(spider, self.request, self.response)
            self._corrupt_cache_entry(mw.storage, spider, self.request)

            caplog.clear()
            with caplog.at_level(logging.WARNING):
                assert mw.process_request(self.request) is None

            assert "treating it as a cache miss" in caplog.text
            assert mw.crawler.stats.get_value("httpcache/retrieve_error") == 1
            assert mw.crawler.stats.get_value("httpcache/miss") == 1

            # Storing the response again replaces the corrupted cache entry.
            mw.storage.store_response(spider, self.request, self.response)
            self.assertEqualResponse(
                self.response, mw.storage.retrieve_response(spider, self.request)
            )

    def test_corrupted_cache_entry_ignore_missing(self):
        with self._middleware(HTTPCACHE_IGNORE_MISSING=True) as mw:
            spider = mw.crawler.spider
            assert spider
            mw.storage.store_response(spider, self.request, self.response)
            self._corrupt_cache_entry(mw.storage, spider, self.request)

            with pytest.raises(IgnoreRequest):
                mw.process_request(self.request)

            assert mw.crawler.stats.get_value("httpcache/retrieve_error") == 1
            assert mw.crawler.stats.get_value("httpcache/ignore") == 1

    def test_storage_no_content_type_header(self):
        """Test that the response body is used to get the right response class
        even if there is no Content-Type header"""
        with self._storage() as (storage, crawler):
            assert storage.retrieve_response(crawler.spider, self.request) is None
            response = Response(
                "http://www.example.com",
                body=b"<!DOCTYPE html>\n<title>.</title>",
                status=202,
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, HtmlResponse)
            self.assertEqualResponse(response, cached_response)

    def test_storage_response_class(self):
        with self._storage() as (storage, crawler):
            response = CustomResponse(
                "http://www.example.com", body=b"test body", custom="value"
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, CustomResponse)
            assert cached_response.custom == "value"

    def test_storage_encoding(self):
        """The encoding of the stored response is kept even when it cannot be
        inferred from the response data."""
        with self._storage() as (storage, crawler):
            response = HtmlResponse(
                "http://www.example.com",
                body='<meta charset="iso-8859-1">€'.encode(),
                encoding="utf-8",
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert cached_response.encoding == "utf-8"
            assert cached_response.text == response.text

    def test_storage_old_cache_entry(self):
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            self._downgrade_cache_entry(storage, crawler.spider, self.request)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, HtmlResponse)
            self.assertEqualResponse(self.response, cached_response)

    def test_storage_vary_variants(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        req_zh = Request(url, headers={"Accept-Language": "zh"})
        res_en = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        res_zh = Response(url, headers={"Vary": "Accept-Language"}, body=b"zh body")
        with self._storage() as (storage, crawler):
            assert storage.retrieve_response(crawler.spider, req_en) is None
            storage.store_response(crawler.spider, req_en, res_en)
            storage.store_response(crawler.spider, req_zh, res_zh)
            self.assertEqualResponse(
                res_en, storage.retrieve_response(crawler.spider, req_en)
            )
            self.assertEqualResponse(
                res_zh, storage.retrieve_response(crawler.spider, req_zh)
            )
            # A request whose selecting header values match no stored
            # variant is a miss.
            req_fr = Request(url, headers={"Accept-Language": "fr"})
            assert storage.retrieve_response(crawler.spider, req_fr) is None

    def test_storage_vary_header_names_case_insensitive(self):
        url = "http://www.example.com/vary"
        req = Request(url, headers={"accept-language": "en"})
        res = Response(url, headers={"VARY": "ACCEPT-LANGUAGE"}, body=b"en body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req, res)
            req2 = Request(url, headers={"Accept-Language": "en"})
            self.assertEqualResponse(
                res, storage.retrieve_response(crawler.spider, req2)
            )

    def test_storage_vary_missing_vs_empty_header(self):
        url = "http://www.example.com/vary"
        req_empty = Request(url, headers={"Accept-Language": ""})
        res = Response(url, headers={"Vary": "Accept-Language"}, body=b"empty body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req_empty, res)
            self.assertEqualResponse(
                res, storage.retrieve_response(crawler.spider, req_empty)
            )
            # A missing selecting header is not the same as an empty one.
            req_absent = Request(url)
            assert storage.retrieve_response(crawler.spider, req_absent) is None

    def test_storage_vary_star_not_stored(self):
        url = "http://www.example.com/vary"
        req = Request(url)
        res = Response(url, headers={"Vary": "*"}, body=b"body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req, res)
            assert storage.retrieve_response(crawler.spider, req) is None

    def test_storage_vary_set_change_invalidates(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        res_v1 = Response(url, headers={"Vary": "Accept-Language"}, body=b"v1")
        res_v2 = Response(url, headers={"Vary": "Accept-Encoding"}, body=b"v2")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req_en, res_v1)
            storage.store_response(crawler.spider, req_en, res_v2)
            # The new Vary set replaces the variants stored before.
            self.assertEqualResponse(
                res_v2, storage.retrieve_response(crawler.spider, req_en)
            )
            req_gzip = Request(
                url, headers={"Accept-Language": "en", "Accept-Encoding": "gzip"}
            )
            assert storage.retrieve_response(crawler.spider, req_gzip) is None

    def test_storage_vary_removed_invalidates(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        req_zh = Request(url, headers={"Accept-Language": "zh"})
        res_en = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        res_plain = Response(url, body=b"plain body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req_en, res_en)
            storage.store_response(crawler.spider, req_en, res_plain)
            # A response without Vary applies to every request, the variants
            # stored before are gone.
            self.assertEqualResponse(
                res_plain, storage.retrieve_response(crawler.spider, req_zh)
            )

    def test_storage_vary_supersedes_no_vary_entry(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        res_plain = Response(url, body=b"plain body")
        res_en = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req_en, res_plain)
            storage.store_response(crawler.spider, req_en, res_en)
            # The no-vary entry stored before is superseded by the variant.
            self.assertEqualResponse(
                res_en, storage.retrieve_response(crawler.spider, req_en)
            )
            req_fr = Request(url, headers={"Accept-Language": "fr"})
            assert storage.retrieve_response(crawler.spider, req_fr) is None

    def test_storage_vary_expiration(self):
        url = "http://www.example.com/vary"
        req = Request(url, headers={"Accept-Language": "en"})
        res = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        with self._storage(HTTPCACHE_EXPIRATION_SECS=100) as (storage, crawler):
            storage.store_response(crawler.spider, req, res)
            expired = time.time() + 200
            with mock.patch("scrapy.extensions.httpcache.time", return_value=expired):
                assert storage.retrieve_response(crawler.spider, req) is None

    def test_storage_vary_missing_variant_metadata_is_a_miss(self):
        url = "http://www.example.com/vary"
        req = Request(url, headers={"Accept-Language": "en"})
        res = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req, res)
            self._delete_vary_variant_metadata(storage, crawler.spider, req)
            assert storage.retrieve_response(crawler.spider, req) is None

    def test_storage_vary_restart(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        req_zh = Request(url, headers={"Accept-Language": "zh"})
        res_en = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        res_zh = Response(url, headers={"Vary": "Accept-Language"}, body=b"zh body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req_en, res_en)
            storage.store_response(crawler.spider, req_zh, res_zh)
        # A reopened storage finds the right variants.
        with self._storage() as (storage, crawler):
            self.assertEqualResponse(
                res_en, storage.retrieve_response(crawler.spider, req_en)
            )
            self.assertEqualResponse(
                res_zh, storage.retrieve_response(crawler.spider, req_zh)
            )

    def test_storage_old_cache_entry_with_vary_is_a_miss(self):
        url = "http://www.example.com/vary"
        req = Request(url, headers={"Accept-Language": "en"})
        res = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, req, res)
            self._downgrade_vary_cache_entry(storage, crawler.spider, req)
            # Without variant metadata there is no way to tell which
            # requests the stored response can answer.
            assert storage.retrieve_response(crawler.spider, req) is None


class PolicyTestMixin(TestBase):
    """Mixin containing policy-specific test methods."""

    def test_cache_timestamp(self):
        with self._middleware() as mw:
            assert mw.process_request(self.request) is None
            assert "cache_timestamp" not in self.request.meta

            before = time.time()
            mw.process_response(self.request, self.response)
            after = time.time()

            if not mw.policy.should_cache_response(self.response, self.request):
                return

            response = mw.process_request(self.request)
            assert isinstance(response, Response)
            assert before <= self.request.meta["cache_timestamp"] <= after

    def test_dont_cache(self):
        with self._middleware() as mw:
            self.request.meta["dont_cache"] = True
            assert mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)
            assert mw.storage.retrieve_response(mw.crawler.spider, self.request) is None

        with self._middleware() as mw:
            self.request.meta["dont_cache"] = False
            mw.process_response(self.request, self.response)
            if mw.policy.should_cache_response(self.response, self.request):
                assert isinstance(
                    mw.storage.retrieve_response(mw.crawler.spider, self.request),
                    self.response.__class__,
                )


class DummyPolicyTestMixin(PolicyTestMixin):
    """Mixin containing dummy policy specific test methods."""

    def test_middleware(self):
        with self._middleware() as mw:
            assert mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)
            response = mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    def test_different_request_response_urls(self):
        with self._middleware() as mw:
            req = Request("http://host.com/path")
            res = Response("http://host2.net/test.html")
            assert mw.process_request(req) is None
            mw.process_response(req, res)
            cached = mw.process_request(req)
            assert isinstance(cached, Response)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

    def test_middleware_ignore_missing(self):
        with self._middleware(HTTPCACHE_IGNORE_MISSING=True) as mw:
            with pytest.raises(IgnoreRequest):
                mw.process_request(self.request)
            mw.process_response(self.request, self.response)
            response = mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    def test_middleware_ignore_schemes(self):
        # http responses are cached by default
        req, res = Request("http://test.com/"), Response("http://test.com/")
        with self._middleware() as mw:
            assert mw.process_request(req) is None
            mw.process_response(req, res)

            cached = mw.process_request(req)
            assert isinstance(cached, Response), type(cached)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

        # file response is not cached by default
        req, res = Request("file:///tmp/t.txt"), Response("file:///tmp/t.txt")
        with self._middleware() as mw:
            assert mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert mw.process_request(req) is None

        # s3 scheme response is cached by default
        req, res = Request("s3://bucket/key"), Response("s3://bucket/key")
        with self._middleware() as mw:
            assert mw.process_request(req) is None
            mw.process_response(req, res)

            cached = mw.process_request(req)
            assert isinstance(cached, Response), type(cached)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

        # ignore s3 scheme
        req, res = Request("s3://bucket/key2"), Response("s3://bucket/key2")
        with self._middleware(HTTPCACHE_IGNORE_SCHEMES=["s3"]) as mw:
            assert mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert mw.process_request(req) is None

    def test_middleware_ignore_http_codes(self):
        # test response is not cached
        with self._middleware(HTTPCACHE_IGNORE_HTTP_CODES=[202]) as mw:
            assert mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)

            assert mw.storage.retrieve_response(mw.crawler.spider, self.request) is None
            assert mw.process_request(self.request) is None

        # test response is cached
        with self._middleware(HTTPCACHE_IGNORE_HTTP_CODES=[203]) as mw:
            mw.process_response(self.request, self.response)
            response = mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    def test_revalidation_keeps_cached_response(self):
        # The dummy policy considers every cached response valid, so a policy
        # that subclasses it to force revalidation always gets the cached
        # response back, whatever the new response is.
        with self._middleware(HTTPCACHE_POLICY=AlwaysStalePolicy) as mw:
            assert mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)

            assert mw.process_request(self.request) is None
            fresh_response = self.response.replace(body=b"new body")
            response = mw.process_response(self.request, fresh_response)
            assert isinstance(response, Response)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags
            assert mw.stats.get_value("httpcache/revalidate") == 1

    def test_middleware_vary(self):
        url = "http://www.example.com/vary"
        req_en = Request(url, headers={"Accept-Language": "en"})
        req_zh = Request(url, headers={"Accept-Language": "zh"})
        res_en = Response(url, headers={"Vary": "Accept-Language"}, body=b"en body")
        res_zh = Response(url, headers={"Vary": "Accept-Language"}, body=b"zh body")
        with self._middleware() as mw:
            assert mw.process_request(req_en) is None
            mw.process_response(req_en, res_en)
            assert mw.process_request(req_zh) is None
            mw.process_response(req_zh, res_zh)
            cached_en = mw.process_request(req_en)
            assert isinstance(cached_en, Response)
            assert cached_en.body == b"en body"
            cached_zh = mw.process_request(req_zh)
            assert isinstance(cached_zh, Response)
            assert cached_zh.body == b"zh body"


class RFC2616PolicyTestMixin(PolicyTestMixin):
    """Mixin containing RFC2616 policy specific test methods."""

    @staticmethod
    def _process_requestresponse(
        mw: HttpCacheMiddleware, request: Request, response: Response | None
    ) -> Response:
        result: Request | Response | None = None
        try:
            result = mw.process_request(request)
            if result:
                assert isinstance(result, Response)
                return result
            assert response is not None
            result = mw.process_response(request, response)
            assert isinstance(result, Response)
            return result
        except Exception:
            print("Request", request)
            print("Response", response)
            print("Result", result)
            raise

    def test_request_cacheability(self):
        res0 = Response(
            self.request.url, status=200, headers={"Expires": self.tomorrow}
        )
        req0 = Request("http://example.com")
        req1 = req0.replace(headers={"Cache-Control": "no-store"})
        req2 = req0.replace(headers={"Cache-Control": "no-cache"})
        with self._middleware() as mw:
            # response for a request with no-store must not be cached
            res1 = self._process_requestresponse(mw, req1, res0)
            self.assertEqualResponse(res1, res0)
            assert mw.storage.retrieve_response(mw.crawler.spider, req1) is None
            # Re-do request without no-store and expect it to be cached
            res2 = self._process_requestresponse(mw, req0, res0)
            assert "cached" not in res2.flags
            res3 = mw.process_request(req0)
            assert isinstance(res3, Response)
            assert "cached" in res3.flags
            self.assertEqualResponse(res2, res3)
            # request with no-cache directive must not return cached response
            # but it allows new response to be stored
            res0b = res0.replace(body=b"foo")
            res4 = self._process_requestresponse(mw, req2, res0b)
            self.assertEqualResponse(res4, res0b)
            assert "cached" not in res4.flags
            res5 = self._process_requestresponse(mw, req0, None)
            self.assertEqualResponse(res5, res0b)
            assert "cached" in res5.flags

    def test_response_cacheability(self):
        responses = [
            # 304 is not cacheable no matter what servers sends
            (False, 304, {}),
            (False, 304, {"Last-Modified": self.yesterday}),
            (False, 304, {"Expires": self.tomorrow}),
            (False, 304, {"Etag": "bar"}),
            (False, 304, {"Cache-Control": "max-age=3600"}),
            # Always obey no-store cache control
            (False, 200, {"Cache-Control": "no-store"}),
            (False, 200, {"Cache-Control": "no-store, max-age=300"}),  # invalid
            (
                False,
                200,
                {"Cache-Control": "no-store", "Expires": self.tomorrow},
            ),  # invalid
            # Ignore responses missing expiration and/or validation headers
            (False, 200, {}),
            (False, 302, {}),
            (False, 307, {}),
            (False, 404, {}),
            # Cache responses with expiration and/or validation headers
            (True, 200, {"Last-Modified": self.yesterday}),
            (True, 203, {"Last-Modified": self.yesterday}),
            (True, 300, {"Last-Modified": self.yesterday}),
            (True, 301, {"Last-Modified": self.yesterday}),
            (True, 308, {"Last-Modified": self.yesterday}),
            (True, 401, {"Last-Modified": self.yesterday}),
            (True, 404, {"Cache-Control": "public, max-age=600"}),
            (True, 302, {"Expires": self.tomorrow}),
            (True, 200, {"Etag": "foo"}),
        ]
        with self._middleware() as mw:
            for idx, (shouldcache, status, headers) in enumerate(responses):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                res1 = self._process_requestresponse(mw, req0, res0)
                res304 = res0.replace(status=304)
                res2 = self._process_requestresponse(
                    mw, req0, res304 if shouldcache else res0
                )
                self.assertEqualResponse(res1, res0)
                self.assertEqualResponse(res2, res0)
                resc = mw.storage.retrieve_response(mw.crawler.spider, req0)
                if shouldcache:
                    self.assertEqualResponse(resc, res1)
                    assert "cached" in res2.flags
                    assert res2.status != 304
                else:
                    assert not resc
                    assert "cached" not in res2.flags

        # cache unconditionally unless response contains no-store or is a 304
        with self._middleware(HTTPCACHE_ALWAYS_STORE=True) as mw:
            for idx, (_, status, headers) in enumerate(responses):
                shouldcache = (
                    "no-store" not in headers.get("Cache-Control", "") and status != 304
                )
                req0 = Request(f"http://example2-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                res1 = self._process_requestresponse(mw, req0, res0)
                res304 = res0.replace(status=304)
                res2 = self._process_requestresponse(
                    mw, req0, res304 if shouldcache else res0
                )
                self.assertEqualResponse(res1, res0)
                self.assertEqualResponse(res2, res0)
                resc = mw.storage.retrieve_response(mw.crawler.spider, req0)
                if shouldcache:
                    self.assertEqualResponse(resc, res1)
                    assert "cached" in res2.flags
                    assert res2.status != 304
                else:
                    assert not resc
                    assert "cached" not in res2.flags

    def test_cached_and_fresh(self):
        sampledata = [
            (200, {"Date": self.yesterday, "Expires": self.tomorrow}),
            (200, {"Date": self.yesterday, "Cache-Control": "max-age=86405"}),
            (200, {"Age": "299", "Cache-Control": "max-age=300"}),
            # Obey max-age if present over any others
            (
                200,
                {
                    "Date": self.today,
                    "Age": "86405",
                    "Cache-Control": "max-age=" + str(86400 * 3),
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            # obey Expires if max-age is not present
            (
                200,
                {
                    "Date": self.yesterday,
                    "Age": "86400",
                    "Cache-Control": "public",
                    "Expires": self.tomorrow,
                    "Last-Modified": self.yesterday,
                },
            ),
            # Default missing Date header to right now
            (200, {"Expires": self.tomorrow}),
            # Firefox - Expires if age is greater than 10% of (Date - Last-Modified)
            (
                200,
                {
                    "Date": self.today,
                    "Last-Modified": self.yesterday,
                    "Age": str(86400 / 10 - 1),
                },
            ),
            # Firefox - Set one year maxage to permanent redirects missing expiration info
            (300, {}),
            (301, {}),
            (308, {}),
        ]
        with self._middleware() as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                # cache fresh response
                res1 = self._process_requestresponse(mw, req0, res0)
                self.assertEqualResponse(res1, res0)
                assert "cached" not in res1.flags
                # return fresh cached response without network interaction
                res2 = self._process_requestresponse(mw, req0, None)
                self.assertEqualResponse(res1, res2)
                assert "cached" in res2.flags
                # validate cached response if request max-age set as 0
                req1 = req0.replace(headers={"Cache-Control": "max-age=0"})
                res304 = res0.replace(status=304)
                assert mw.process_request(req1) is None
                res3 = self._process_requestresponse(mw, req1, res304)
                self.assertEqualResponse(res1, res3)
                assert "cached" in res3.flags

    def test_cached_and_stale(self):
        sampledata = [
            (200, {"Date": self.today, "Expires": self.yesterday}),
            (
                200,
                {
                    "Date": self.today,
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            (200, {"Expires": self.yesterday}),
            (200, {"Expires": self.yesterday, "ETag": "foo"}),
            (200, {"Expires": self.yesterday, "Last-Modified": self.yesterday}),
            (200, {"Expires": self.tomorrow, "Age": "86405"}),
            (200, {"Cache-Control": "max-age=86400", "Age": "86405"}),
            # no-cache forces expiration, also revalidation if validators exists
            (200, {"Cache-Control": "no-cache"}),
            (200, {"Cache-Control": "no-cache", "ETag": "foo"}),
            (200, {"Cache-Control": "no-cache", "Last-Modified": self.yesterday}),
            (
                200,
                {
                    "Cache-Control": "no-cache,must-revalidate",
                    "Last-Modified": self.yesterday,
                },
            ),
            (
                200,
                {
                    "Cache-Control": "must-revalidate",
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            (200, {"Cache-Control": "max-age=86400,must-revalidate", "Age": "86405"}),
        ]
        with self._middleware() as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0a = Response(req0.url, status=status, headers=headers)
                # cache expired response
                res1 = self._process_requestresponse(mw, req0, res0a)
                self.assertEqualResponse(res1, res0a)
                assert "cached" not in res1.flags
                # Same request but as cached response is stale a new response must
                # be returned
                res0b = res0a.replace(body=b"bar")
                res2 = self._process_requestresponse(mw, req0, res0b)
                self.assertEqualResponse(res2, res0b)
                assert "cached" not in res2.flags
                cc = headers.get("Cache-Control", "")
                # Previous response expired too, subsequent request to same
                # resource must revalidate and succeed on 304 if validators
                # are present
                if "ETag" in headers or "Last-Modified" in headers:
                    res0c = res0b.replace(status=304)
                    res3 = self._process_requestresponse(mw, req0, res0c)
                    self.assertEqualResponse(res3, res0b)
                    assert "cached" in res3.flags
                    # get cached response on server errors unless must-revalidate
                    # in cached response
                    res0d = res0b.replace(status=500)
                    res4 = self._process_requestresponse(mw, req0, res0d)
                    if "must-revalidate" in cc:
                        assert "cached" not in res4.flags
                        self.assertEqualResponse(res4, res0d)
                    else:
                        assert "cached" in res4.flags
                        self.assertEqualResponse(res4, res0b)
                # Requests with max-stale can fetch expired cached responses
                # unless cached response has must-revalidate
                req1 = req0.replace(headers={"Cache-Control": "max-stale"})
                res5 = self._process_requestresponse(mw, req1, res0b)
                self.assertEqualResponse(res5, res0b)
                if "no-cache" in cc or "must-revalidate" in cc:
                    assert "cached" not in res5.flags
                else:
                    assert "cached" in res5.flags

    def test_middleware_ignore_schemes(self):
        # file responses are not cached by default
        req = Request("file:///tmp/t.txt")
        res = Response(req.url, headers={"Expires": self.tomorrow})
        with self._middleware() as mw:
            assert mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert mw.process_request(req) is None

    def test_max_stale_with_value(self):
        # A response that expired one day ago.
        headers = {"Date": self.yesterday, "Expires": self.yesterday}
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            self._process_requestresponse(mw, req0, res0)

            # max-stale greater than the staleness of the cached response
            req1 = req0.replace(headers={"Cache-Control": "max-stale=172800"})
            res1 = mw.process_request(req1)
            assert isinstance(res1, Response)
            assert "cached" in res1.flags

            # max-stale lower than the staleness of the cached response
            req2 = req0.replace(headers={"Cache-Control": "max-stale=60"})
            assert mw.process_request(req2) is None

            # a non-integer max-stale value is ignored
            req3 = req0.replace(headers={"Cache-Control": "max-stale=soon"})
            assert mw.process_request(req3) is None

    def test_freshen_response_on_revalidation(self):
        # A response successfully revalidated (304) must have its Date
        # header, and thus its age, refreshed in the cache.
        headers = {
            "Date": self.yesterday,
            "Cache-Control": "max-age=60",
            "Last-Modified": self.yesterday,
            "Warning": ["110 - old", "214 - keep"],
        }
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            self._process_requestresponse(mw, req0, res0)

            # The cached response is stale, so the request must be
            # revalidated with the server.
            assert mw.process_request(req0) is None
            res304 = Response(req0.url, status=304, headers={"Date": self.today})
            res1 = mw.process_response(req0, res304)
            assert res1.headers["Date"] == self.today.encode()
            assert res1.headers["Warning"] == b"214 - keep"

            # The freshened response must now be considered fresh again,
            # without a further round trip to the server.
            res2 = mw.process_request(req0)
            assert isinstance(res2, Response)
            assert "cached" in res2.flags
            assert res2.headers["Date"] == self.today.encode()

    def test_response_dated_in_the_future(self):
        # A Date header ahead of the local clock must not make the cached
        # response look aged.
        headers = {"Date": self.tomorrow, "Cache-Control": "max-age=10"}
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            res1 = self._process_requestresponse(mw, req0, res0)
            assert "cached" not in res1.flags

            res2 = self._process_requestresponse(mw, req0, None)
            self.assertEqualResponse(res1, res2)
            assert "cached" in res2.flags

    def test_cache_timestamp_not_leaked_on_invalidate(self):
        # A stale cached response invalidated by a fresh network response must
        # not leave its storage timestamp behind on the request meta.
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers={"Expires": self.yesterday})
            self._process_requestresponse(mw, req0, res0)

            assert mw.process_request(req0) is None
            assert "cache_timestamp" in req0.meta

            res1 = res0.replace(body=b"new body")
            response = mw.process_response(req0, res1)
            assert response is res1
            assert "cache_timestamp" not in req0.meta

    def test_process_exception(self):
        with self._middleware() as mw:
            res0 = Response(self.request.url, headers={"Expires": self.yesterday})
            req0 = Request(self.request.url)
            self._process_requestresponse(mw, req0, res0)
            for e in mw.DOWNLOAD_EXCEPTIONS:
                # Simulate encountering an error on download attempts
                assert mw.process_request(req0) is None
                res1 = mw.process_exception(req0, e("foo"))
                # Use cached response as recovery
                assert isinstance(res1, Response)
                assert "cached" in res1.flags
                self.assertEqualResponse(res0, res1)
            # Do not use cached response for unhandled exceptions
            mw.process_request(req0)
            assert mw.process_exception(req0, Exception("foo")) is None

    def test_ignore_response_cache_controls(self):
        sampledata = [
            (200, {"Date": self.yesterday, "Expires": self.tomorrow}),
            (200, {"Date": self.yesterday, "Cache-Control": "no-store,max-age=86405"}),
            (200, {"Age": "299", "Cache-Control": "max-age=300,no-cache"}),
            (300, {"Cache-Control": "no-cache"}),
            (200, {"Expires": self.tomorrow, "Cache-Control": "no-store"}),
        ]
        with self._middleware(
            HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS=["no-cache", "no-store"]
        ) as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                # cache fresh response
                res1 = self._process_requestresponse(mw, req0, res0)
                self.assertEqualResponse(res1, res0)
                assert "cached" not in res1.flags
                # return fresh cached response without network interaction
                res2 = self._process_requestresponse(mw, req0, None)
                self.assertEqualResponse(res1, res2)
                assert "cached" in res2.flags

    def test_vary_variants(self):
        url = "http://example.com/vary"
        res_en = Response(
            url,
            status=200,
            body=b"English",
            headers={
                "Date": self.today,
                "Cache-Control": "max-age=3600",
                "ETag": '"en"',
                "Vary": "Accept-Language",
            },
        )
        res_zh = Response(
            url,
            status=200,
            body="中文".encode(),
            headers={
                "Date": self.today,
                "Cache-Control": "max-age=3600",
                "ETag": '"zh"',
                "Vary": "Accept-Language",
            },
        )
        with self._middleware() as mw:
            req_en = Request(url, headers={"Accept-Language": "en"})
            req_zh = Request(url, headers={"Accept-Language": "zh"})
            # store both variants
            assert mw.process_request(req_en) is None
            mw.process_response(req_en, res_en)
            assert mw.process_request(req_zh) is None
            mw.process_response(req_zh, res_zh)
            # each request hits its own variant
            cached_en = mw.process_request(req_en)
            assert isinstance(cached_en, Response)
            assert cached_en.body == b"English"
            assert "cached" in cached_en.flags
            cached_zh = mw.process_request(req_zh)
            assert isinstance(cached_zh, Response)
            assert cached_zh.body == "中文".encode()
            # a stale request revalidates its own variant with its ETag
            req = Request(
                url, headers={"Accept-Language": "en", "Cache-Control": "max-age=0"}
            )
            assert mw.process_request(req) is None
            assert req.headers[b"If-None-Match"] == b'"en"'
            res304 = Response(
                url,
                status=304,
                headers={"Date": self.tomorrow, "Vary": "Accept-Language"},
            )
            revalidated = mw.process_response(req, res304)
            # the body comes from the cache, the headers are merged
            assert isinstance(revalidated, Response)
            assert revalidated.body == b"English"
            assert revalidated.headers[b"Date"] == self.tomorrow.encode()
            # the other variant is still cached
            cached_zh2 = mw.process_request(req_zh)
            assert isinstance(cached_zh2, Response)
            assert cached_zh2.body == "中文".encode()

    def test_vary_change_on_revalidation(self):
        url = "http://example.com/vary"
        res_en = Response(
            url,
            status=200,
            body=b"English",
            headers={
                "Date": self.today,
                "Cache-Control": "max-age=3600",
                "ETag": '"en"',
                "Vary": "Accept-Language",
            },
        )
        res_zh = Response(
            url,
            status=200,
            body="中文".encode(),
            headers={
                "Date": self.today,
                "Cache-Control": "max-age=3600",
                "ETag": '"zh"',
                "Vary": "Accept-Language",
            },
        )
        with self._middleware() as mw:
            req_en = Request(url, headers={"Accept-Language": "en"})
            req_zh = Request(url, headers={"Accept-Language": "zh"})
            assert mw.process_request(req_en) is None
            mw.process_response(req_en, res_en)
            assert mw.process_request(req_zh) is None
            mw.process_response(req_zh, res_zh)
            # a 304 that changes the Vary set invalidates the old variants
            req = Request(
                url, headers={"Accept-Language": "en", "Cache-Control": "max-age=0"}
            )
            assert mw.process_request(req) is None
            res304 = Response(
                url,
                status=304,
                headers={"Date": self.tomorrow, "Vary": "Accept-Encoding"},
            )
            revalidated = mw.process_response(req, res304)
            assert isinstance(revalidated, Response)
            assert revalidated.body == b"English"
            # the old Accept-Language variants cannot be hit anymore
            req_zh_gzip = Request(
                url, headers={"Accept-Language": "zh", "Accept-Encoding": "gzip"}
            )
            assert mw.process_request(req_zh_gzip) is None
            # requests now match on Accept-Encoding only
            cached = mw.process_request(req_zh)
            assert isinstance(cached, Response)
            assert cached.body == b"English"


# Concrete test classes that combine storage and policy mixins


class FilesystemStorageTestMixin(StorageTestMixin):
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        (rpath / "response_body").unlink()

    def _downgrade_cache_entry(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        (rpath / "response_data").unlink()

    def _downgrade_vary_cache_entry(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        variant_dirs = [p for p in rpath.iterdir() if p.is_dir()]
        assert len(variant_dirs) == 1
        for file in variant_dirs[0].iterdir():
            file.rename(rpath / file.name)
        variant_dirs[0].rmdir()
        (rpath / "vary_index").unlink()

    def _delete_vary_variant_metadata(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        for metapath in rpath.glob("vary_*/pickled_meta"):
            metapath.unlink()


class DbmStorageTestMixin(StorageTestMixin):
    storage_class = "scrapy.extensions.httpcache.DbmCacheStorage"

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        storage.db[f"{key}_data"] = b"not a pickle"

    def _downgrade_cache_entry(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        data = pickle.loads(storage.db[f"{key}_data"])
        data = {
            k: v for k, v in data.items() if k in ("status", "url", "headers", "body")
        }
        storage.db[f"{key}_data"] = pickle.dumps(data, protocol=4)

    def _downgrade_vary_cache_entry(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        index = pickle.loads(storage.db[f"{key}_vary_index"])
        assert len(index) == 1
        prefix = f"{key}_vary_{index[0]['key']}"
        storage.db[f"{key}_data"] = storage.db[f"{prefix}_data"]
        storage.db[f"{key}_time"] = storage.db[f"{prefix}_time"]
        del storage.db[f"{prefix}_data"]
        del storage.db[f"{prefix}_time"]
        del storage.db[f"{key}_vary_index"]

    def _delete_vary_variant_metadata(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        index = pickle.loads(storage.db[f"{key}_vary_index"])
        assert len(index) == 1
        del storage.db[f"{key}_vary_{index[0]['key']}_time"]


class TestFilesystemStorageWithDummyPolicy(
    FilesystemStorageTestMixin, DummyPolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"


class TestFilesystemStorageWithRFC2616Policy(
    FilesystemStorageTestMixin, RFC2616PolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.RFC2616Policy"


class TestDbmStorageWithDummyPolicy(DbmStorageTestMixin, DummyPolicyTestMixin):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"


class TestDbmStorageWithRFC2616Policy(DbmStorageTestMixin, RFC2616PolicyTestMixin):
    policy_class = "scrapy.extensions.httpcache.RFC2616Policy"


class TestDbmStorageWithCustomDbmModule(TestDbmStorageWithDummyPolicy):
    dbm_module = "tests.mocks.dummydbm"

    def _get_settings(self, **new_settings) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_DBM_MODULE", self.dbm_module)
        return super()._get_settings(**new_settings)

    def test_custom_dbm_module_loaded(self):
        # make sure our dbm module has been loaded
        with self._storage() as (storage, _):
            assert storage.dbmodule.__name__ == self.dbm_module


class TestFilesystemStorageGzipWithDummyPolicy(TestFilesystemStorageWithDummyPolicy):
    def _get_settings(self, **new_settings) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_GZIP", True)
        return super()._get_settings(**new_settings)

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        # A spider killed while writing a gzip file leaves it truncated.
        body_path = Path(storage._get_request_path(spider, request), "response_body")
        body_path.write_bytes(body_path.read_bytes()[:-5])


@pytest.mark.parametrize(
    ("string", "expected"),
    [
        # RFC examples
        ("Sun, 06 Nov 1994 08:49:37 GMT", 784111777),
        ("Sunday, 06-Nov-94 08:49:37 GMT", 784111777),
        ("Sun Nov  6 08:49:37 1994", 784111777),
        (None, None),
        ("foo", None),
    ],
)
def test_rfc1123_to_epoch(string: str | None, expected: int | None) -> None:
    assert rfc1123_to_epoch(string) == expected


class VarySpider(MockServerSpider):
    """Request the /vary mock server resource once per step, in order."""

    name = "vary-spider"

    def __init__(self, *args: Any, steps: list[dict[str, str]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.steps = steps
        self.responses: list[Response] = []

    async def start(self) -> AsyncIterator[Any]:
        yield self._request(0)

    def _request(self, step: int) -> Request:
        assert self.mockserver is not None
        return Request(
            self.mockserver.url("/vary"),
            headers=self.steps[step],
            dont_filter=True,
            callback=self.parse,
            meta={"step": step},
        )

    def parse(self, response: Response) -> Any:
        self.responses.append(response)
        step = response.meta["step"] + 1
        if step < len(self.steps):
            yield self._request(step)


class TestHttpCacheVaryMockServer:
    @pytest.mark.parametrize(
        "storage_class",
        [
            "scrapy.extensions.httpcache.FilesystemCacheStorage",
            "scrapy.extensions.httpcache.DbmCacheStorage",
        ],
    )
    @coroutine_test
    async def test_vary(
        self, mockserver: MockServer, tmp_path: Path, storage_class: str
    ) -> None:
        settings = {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": str(tmp_path),
            "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy",
            "HTTPCACHE_STORAGE": storage_class,
        }
        steps = [
            {"Accept-Language": "en"},  # miss, stored
            {"Accept-Language": "en"},  # fresh hit
            # stale, revalidated with the ETag of the en variant
            {"Accept-Language": "en", "Cache-Control": "max-age=0"},
            {"Accept-Language": "zh"},  # miss, stored
            {"Accept-Language": "zh"},  # fresh hit
        ]
        crawler = get_crawler(VarySpider, settings)
        await crawler.crawl_async(mockserver=mockserver, steps=steps)
        assert crawler.spider is not None
        assert isinstance(crawler.spider, VarySpider)
        responses = crawler.spider.responses
        assert [r.body for r in responses] == [
            b"English",
            b"English",
            b"English",
            "中文".encode(),
            "中文".encode(),
        ]
        assert "cached" not in responses[0].flags
        assert "cached" in responses[1].flags
        # the revalidation sent the ETag of the matching variant
        assert responses[2].headers["X-Received-If-None-Match"] == b'"lang-en"'
        stats = crawler.stats
        assert stats.get_value("httpcache/miss") == 2
        assert stats.get_value("httpcache/hit") == 2
        assert stats.get_value("httpcache/revalidate") == 1
        assert stats.get_value("httpcache/store") == 3

        # a new crawler on the same cache directory finds the variants
        crawler2 = get_crawler(VarySpider, settings)
        await crawler2.crawl_async(
            mockserver=mockserver, steps=[{"Accept-Language": "zh"}]
        )
        assert crawler2.spider is not None
        assert isinstance(crawler2.spider, VarySpider)
        assert crawler2.spider.responses[0].body == "中文".encode()
        assert crawler2.stats.get_value("httpcache/hit") == 1
        assert crawler2.stats.get_value("httpcache/miss") is None
