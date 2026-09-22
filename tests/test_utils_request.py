from __future__ import annotations

import json
from hashlib import sha1, sha256
from typing import TYPE_CHECKING, Any, Protocol, cast
from weakref import WeakKeyDictionary

import pytest

from scrapy.http import Request
from scrapy.utils.python import to_bytes
from scrapy.utils.request import (
    RequestFingerprinter,
    _fingerprint_cache,
    fingerprint,
    request_httprepr,
    request_to_curl,
)
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from collections.abc import Iterable


_FingerprintCacheKey = tuple[
    tuple[bytes, ...] | None,
    bool,
    bool,
    tuple[str, ...] | None,
    tuple[str, ...] | None,
    bytes | None,
]


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (
            Request("http://www.example.com"),
            b"GET / HTTP/1.1\r\nHost: www.example.com\r\n\r\n",
        ),
        (
            Request("http://www.example.com/some/page.html?arg=1"),
            b"GET /some/page.html?arg=1 HTTP/1.1\r\nHost: www.example.com\r\n\r\n",
        ),
        (
            Request(
                "http://www.example.com",
                method="POST",
                headers={"Content-type": b"text/html"},
                body=b"Some body",
            ),
            b"POST / HTTP/1.1\r\nHost: www.example.com\r\nContent-Type: text/html\r\n\r\nSome body",
        ),
    ],
)
def test_request_httprepr(r: Request, expected: bytes) -> None:
    assert request_httprepr(r) == expected


@pytest.mark.parametrize(
    "r",
    [
        Request("file:///tmp/foo.txt"),
        Request("ftp://localhost/tmp/foo.txt"),
    ],
)
def test_request_httprepr_for_non_http_request(r: Request) -> None:
    # the representation is not important but it must not fail.
    request_httprepr(r)


class _FingerprintFunction(Protocol):
    def __call__(
        self,
        request: Request,
        *,
        include_headers: Iterable[bytes | str] | None = None,
        keep_fragments: bool = False,
        keep_query_params: Iterable[bytes | str] | None = None,
        ignore_query_params: Iterable[bytes | str] | None = None,
        scope: bytes | str | None = None,
    ) -> bytes: ...


class TestFingerprint:
    function: _FingerprintFunction = staticmethod(fingerprint)
    cache: WeakKeyDictionary[Request, dict[_FingerprintCacheKey, bytes]] = (
        _fingerprint_cache
    )
    default_cache_key = (None, False, False, None, None, None)
    known_hashes: tuple[tuple[Request, bytes | str, dict[str, Any]], ...] = (
        (
            Request("http://example.org"),
            b"xs\xd7\x0c3uj\x15\xfe\xd7d\x9b\xa9\t\xe0d\xbf\x9cXD",
            {},
        ),
        (
            Request("https://example.org"),
            b"\xc04\x85P,\xaa\x91\x06\xf8t\xb4\xbd*\xd9\xe9\x8a:m\xc3l",
            {},
        ),
        (
            Request("https://example.org?a"),
            b"G\xad\xb8Ck\x19\x1c\xed\x838,\x01\xc4\xde;\xee\xa5\x94a\x0c",
            {},
        ),
        (
            Request("https://example.org?a=b"),
            b"\x024MYb\x8a\xc2\x1e\xbc>\xd6\xac*\xda\x9cF\xc1r\x7f\x17",
            {},
        ),
        (
            Request("https://example.org?a=b&a"),
            b"t+\xe8*\xfb\x84\xe3v\x1a}\x88p\xc0\xccB\xd7\x9d\xfez\x96",
            {},
        ),
        (
            Request("https://example.org?a=b&a=c"),
            b"\xda\x1ec\xd0\x9c\x08s`\xb4\x9b\xe2\xb6R\xf8k\xef\xeaQG\xef",
            {},
        ),
        (
            Request("https://example.org", method="POST"),
            b"\x9d\xcdA\x0fT\x02:\xca\xa0}\x90\xda\x05B\xded\x8aN7\x1d",
            {},
        ),
        (
            Request("https://example.org", body=b"a"),
            b"\xc34z>\xd8\x99\x8b\xda7\x05r\x99I\xa8\xa0x;\xa41_",
            {},
        ),
        (
            Request("https://example.org", method="POST", body=b"a"),
            b"5`\xe2y4\xd0\x9d\xee\xe0\xbatw\x87Q\xe8O\xd78\xfc\xe7",
            {},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"\xc04\x85P,\xaa\x91\x06\xf8t\xb4\xbd*\xd9\xe9\x8a:m\xc3l",
            {},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"]\xc7\x1f\xf2\xafG2\xbc\xa4\xfa\x99\n33\xda\x18\x94\x81U.",
            {"include_headers": ["A"]},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"<\x1a\xeb\x85y\xdeW\xfb\xdcq\x88\xee\xaf\x17\xdd\x0c\xbfH\x18\x1f",
            {"keep_fragments": True},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"\xc1\xef~\x94\x9bS\xc1\x83\t\xdcz8\x9f\xdc{\x11\x16I.\x11",
            {"include_headers": ["A"], "keep_fragments": True},
        ),
        (
            Request("https://example.org/ab"),
            b"N\xe5l\xb8\x12@iw\xe2\xf3\x1bp\xea\xffp!u\xe2\x8a\xc6",
            {},
        ),
        (
            Request("https://example.org/a", body=b"b"),
            b"_NOv\xbco$6\xfcW\x9f\xb24g\x9f\xbb\xdd\xa82\xc5",
            {},
        ),
    )

    def test_query_string_key_order(self):
        r1 = Request("http://www.example.com/query?id=111&cat=222")
        r2 = Request("http://www.example.com/query?cat=222&id=111")
        assert self.function(r1) == self.function(r1)
        assert self.function(r1) == self.function(r2)

    def test_ignore_query_params(self):
        r1 = Request("https://example.com/?id=1&utm_source=first&id=2&empty=&flag")
        r2 = Request(
            "https://example.com/?utm_medium=email&id=2&empty=&id=1&"
            "utm_source=second&flag"
        )
        ignored = ["utm_medium", "utm_source"]

        assert self.function(r1, ignore_query_params=ignored) == self.function(
            r2, ignore_query_params=ignored
        )
        assert self.function(r1, ignore_query_params=ignored) == self.function(
            Request("https://example.com/?id=1&id=2&empty=&flag"),
            ignore_query_params=ignored,
        )
        assert "utm_source=first" in r1.url
        assert "utm_medium=email" in r2.url

    def test_ignore_query_params_matches_decoded_names(self):
        r1 = Request("https://example.com/?utm%5Fsource=first&id=1")
        r2 = Request("https://example.com/?id=1")

        assert self.function(r1, ignore_query_params=["utm_source"]) == self.function(
            r2, ignore_query_params=["utm_source"]
        )

    def test_ignore_query_params_supports_semicolon_separator(self):
        r1 = Request("https://example.com/?id=1;utm_source=first&empty=")
        r2 = Request("https://example.com/?id=1&empty=")

        assert self.function(r1, ignore_query_params=["utm_source"]) == self.function(
            r2, ignore_query_params=["utm_source"]
        )

    def test_keep_query_params(self):
        r1 = Request("https://example.com/?id=1&utm_source=first&id=2&empty=")
        r2 = Request("https://example.com/?utm_medium=email&id=2&empty=&id=1")

        assert self.function(r1, keep_query_params=["empty", "id"]) == self.function(
            r2, keep_query_params=["empty", "id"]
        )

    def test_keep_query_params_empty_list(self):
        r1 = Request("https://example.com/?id=1&empty=")
        r2 = Request("https://example.com/")

        assert self.function(r1, keep_query_params=[]) == self.function(
            r2, keep_query_params=[]
        )

    def test_query_params_keep_and_ignore_are_mutually_exclusive(self):
        request = Request("https://example.com/?id=1&utm_source=first")

        with pytest.raises(ValueError, match="cannot be used together"):
            self.function(
                request,
                keep_query_params=["id"],
                ignore_query_params=["utm_source"],
            )

    def test_scope(self):
        request = Request("https://example.com/?id=1")

        assert self.function(request, scope="account-a") == self.function(
            request, scope="account-a"
        )
        assert self.function(request, scope="account-a") != self.function(
            request, scope="account-b"
        )
        assert self.function(request, scope="account-a") != self.function(request)
        assert request.url == "https://example.com/?id=1"
        assert request.meta == {}

    def test_query_string_key_without_value(self):
        r1 = Request("http://www.example.com/hnnoticiaj1.aspx?78132,199")
        r2 = Request("http://www.example.com/hnnoticiaj1.aspx?78160,199")
        assert self.function(r1) != self.function(r2)

    def test_caching(self):
        r1 = Request("http://www.example.com/hnnoticiaj1.aspx?78160,199")
        assert self.function(r1) == self.cache[r1][self.default_cache_key]

    def test_header(self):
        r1 = Request("http://www.example.com/members/offers.html")
        r2 = Request("http://www.example.com/members/offers.html")
        r2.headers["SESSIONID"] = b"somehash"
        assert self.function(r1) == self.function(r2)

    def test_headers(self):
        r1 = Request("http://www.example.com/")
        r2 = Request("http://www.example.com/")
        r2.headers["Accept-Language"] = b"en"
        r3 = Request("http://www.example.com/")
        r3.headers["Accept-Language"] = b"en"
        r3.headers["SESSIONID"] = b"somehash"

        assert self.function(r1) == self.function(r2) == self.function(r3)

        assert self.function(r1) == self.function(
            r1, include_headers=["Accept-Language"]
        )

        assert self.function(r1) != self.function(
            r2, include_headers=["Accept-Language"]
        )

        assert self.function(
            r3, include_headers=["accept-language", "sessionid"]
        ) == self.function(r3, include_headers=["SESSIONID", "Accept-Language"])

    def test_fragment(self):
        r1 = Request("http://www.example.com/test.html")
        r2 = Request("http://www.example.com/test.html#fragment")
        assert self.function(r1) == self.function(r2)
        assert self.function(r1) == self.function(r1, keep_fragments=True)
        assert self.function(r2) != self.function(r2, keep_fragments=True)
        assert self.function(r1) != self.function(r2, keep_fragments=True)

    def test_verbatim_url(self):
        # verbatim_url requests skip URL canonicalization
        r1 = Request(
            "http://www.example.com/query?a=1&b=2", meta={"verbatim_url": True}
        )
        r2 = Request(
            "http://www.example.com/query?b=2&a=1", meta={"verbatim_url": True}
        )
        assert self.function(r1) != self.function(r2)

        # without verbatim_url, canonicalization makes query-param order irrelevant
        r3 = Request("http://www.example.com/query?a=1&b=2")
        r4 = Request("http://www.example.com/query?b=2&a=1")
        assert self.function(r3) == self.function(r4)

        # with verbatim_url, the fragment is always kept in the fingerprint
        r5 = Request(
            "http://www.example.com/test#fragment", meta={"verbatim_url": True}
        )
        r6 = Request("http://www.example.com/test", meta={"verbatim_url": True})
        assert self.function(r5) != self.function(r6)

        # keep_fragments parameter is ignored for verbatim_url requests
        assert self.function(r5) == self.function(r5, keep_fragments=True)
        assert self.function(r5) == self.function(r5, keep_fragments=False)

    def test_method_and_body(self):
        r1 = Request("http://www.example.com")
        r2 = Request("http://www.example.com", method="POST")
        r3 = Request("http://www.example.com", method="POST", body=b"request body")

        assert self.function(r1) != self.function(r2)
        assert self.function(r2) != self.function(r3)

    def test_request_replace(self):
        # cached fingerprint must be cleared on request copy
        r1 = Request("http://www.example.com")
        fp1 = self.function(r1)
        r2 = r1.replace(url="http://www.example.com/other")
        fp2 = self.function(r2)
        assert fp1 != fp2

    def test_part_separation(self):
        # An old implementation used to serialize request data in a way that
        # would put the body right after the URL.
        r1 = Request("http://www.example.com/foo")
        fp1 = self.function(r1)
        r2 = Request("http://www.example.com/f", body=b"oo")
        fp2 = self.function(r2)
        assert fp1 != fp2

    def test_hashes(self):
        """Test hardcoded hashes, to make sure future changes to not introduce
        backward incompatibilities."""
        actual = [
            self.function(request, **kwargs) for request, _, kwargs in self.known_hashes
        ]
        expected = [_fingerprint for _, _fingerprint, _ in self.known_hashes]
        assert actual == expected


class TestRequestFingerprinter:
    def test_fingerprint(self):
        crawler = get_crawler()
        request = Request("https://example.com")
        assert crawler.request_fingerprinter.fingerprint(request) == fingerprint(
            request
        )

    def test_include_headers_setting(self):
        crawler = get_crawler(
            settings_dict={"REQUEST_FINGERPRINTER_INCLUDE_HEADERS": ["X-Account"]}
        )
        r1 = Request("https://example.com", headers={"X-Account": "a"})
        r2 = Request("https://example.com", headers={"X-Account": "b"})
        r3 = Request(
            "https://example.com",
            headers={"X-Account": "a", "Authorization": "Bearer secret"},
        )

        assert crawler.request_fingerprinter.fingerprint(r1) != (
            crawler.request_fingerprinter.fingerprint(r2)
        )
        assert crawler.request_fingerprinter.fingerprint(r1) == (
            crawler.request_fingerprinter.fingerprint(r3)
        )

    def test_query_params_settings_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="cannot be used together"):
            get_crawler(
                settings_dict={
                    "REQUEST_FINGERPRINTER_KEEP_QUERY_PARAMS": ["id"],
                    "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["utm_source"],
                }
            )

    def test_scope_settings_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="cannot be used together"):
            get_crawler(
                settings_dict={
                    "REQUEST_FINGERPRINTER_SCOPE": "account-a",
                    "REQUEST_FINGERPRINTER_SCOPE_META_KEY": "identity_scope",
                }
            )

    def test_scope_meta_key_setting(self):
        crawler = get_crawler(
            settings_dict={"REQUEST_FINGERPRINTER_SCOPE_META_KEY": "identity_scope"}
        )
        r1 = Request(
            "https://example.com",
            meta={"identity_scope": "account-a", "unrelated": "one"},
        )
        r2 = Request(
            "https://example.com",
            meta={"identity_scope": "account-a", "unrelated": "two"},
        )
        r3 = Request("https://example.com", meta={"identity_scope": "account-b"})

        assert crawler.request_fingerprinter.fingerprint(r1) == (
            crawler.request_fingerprinter.fingerprint(r2)
        )
        assert crawler.request_fingerprinter.fingerprint(r1) != (
            crawler.request_fingerprinter.fingerprint(r3)
        )
        with pytest.raises(ValueError, match="identity_scope"):
            crawler.request_fingerprinter.fingerprint(Request("https://example.com"))

    def test_policy_does_not_include_scope_value(self):
        crawler = get_crawler(
            settings_dict={
                "REQUEST_FINGERPRINTER_INCLUDE_HEADERS": ["X-Account"],
                "REQUEST_FINGERPRINTER_KEEP_QUERY_PARAMS": ["id"],
                "REQUEST_FINGERPRINTER_SCOPE": "account-secret",
            }
        )
        request = Request(
            "https://example.com/?id=1&utm_source=first",
            headers={"X-Account": "a"},
        )

        fingerprinter = cast("RequestFingerprinter", crawler.request_fingerprinter)
        policy = fingerprinter._fingerprint_policy

        assert fingerprinter.fingerprint(request) == fingerprint(
            request,
            include_headers=["X-Account"],
            keep_query_params=["id"],
            scope="account-secret",
        )
        assert policy["scope_setting_sha256"] == sha256(b"account-secret").hexdigest()
        assert "account-secret" not in json.dumps(policy)


class TestCustomRequestFingerprinter:
    def test_include_headers(self):
        class RequestFingerprinter:
            def fingerprint(self, request):
                return fingerprint(request, include_headers=["X-ID"])

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com", headers={"X-ID": "1"})
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com", headers={"X-ID": "2"})
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        assert fp1 != fp2

    def test_dont_canonicalize(self):
        class RequestFingerprinter:
            cache: WeakKeyDictionary[Request, bytes] = WeakKeyDictionary()

            def fingerprint(self, request: Request) -> bytes:
                if request not in self.cache:
                    fp = sha1()
                    fp.update(to_bytes(request.url))
                    self.cache[request] = fp.digest()
                return self.cache[request]

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com?a=1&a=2")
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com?a=2&a=1")
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        assert fp1 != fp2

    def test_meta(self):
        class RequestFingerprinter:
            def fingerprint(self, request):
                if "fingerprint" in request.meta:
                    return request.meta["fingerprint"]
                return fingerprint(request)

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com")
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com", meta={"fingerprint": "a"})
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        r3 = Request("http://www.example.com", meta={"fingerprint": "a"})
        fp3 = crawler.request_fingerprinter.fingerprint(r3)
        r4 = Request("http://www.example.com", meta={"fingerprint": "b"})
        fp4 = crawler.request_fingerprinter.fingerprint(r4)
        assert fp1 != fp2
        assert fp1 != fp4
        assert fp2 != fp4
        assert fp2 == fp3

    def test_from_crawler(self):
        class RequestFingerprinter:
            @classmethod
            def from_crawler(cls, crawler):
                return cls(crawler)

            def __init__(self, crawler):
                self._fingerprint = crawler.settings["FINGERPRINT"]

            def fingerprint(self, request):
                return self._fingerprint

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
            "FINGERPRINT": b"fingerprint",
        }
        crawler = get_crawler(settings_dict=settings)

        request = Request("http://www.example.com")
        fingerprint = crawler.request_fingerprinter.fingerprint(request)
        assert fingerprint == settings["FINGERPRINT"]


class TestRequestToCurl:
    def _test_request(
        self, request_object: Request, expected_curl_command: str
    ) -> None:
        curl_command = request_to_curl(request_object)
        assert curl_command == expected_curl_command

    def test_get(self) -> None:
        request_object = Request("https://www.example.com")
        expected_curl_command = "curl -X GET https://www.example.com"
        self._test_request(request_object, expected_curl_command)

    def test_post(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            'curl -X POST https://www.httpbin.org/post --data-raw \'{"foo": "bar"}\''
        )
        self._test_request(request_object, expected_curl_command)

    def test_headers(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            ' --data-raw \'{"foo": "bar"}\''
            " -H 'Content-Type: application/json' -H 'Accept: application/json'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_dict(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies={"foo": "bar"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_dict_bytes(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies={b"foo": b"bar"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_list_verbose(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies=[
                {
                    "name": b"foo",
                    "value": b"bar",
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_list_verbose_non_string_value(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies=[
                {
                    "name": "foo",
                    "value": 1,
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=1'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_request_to_curl_method(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            'curl -X POST https://www.httpbin.org/post --data-raw \'{"foo": "bar"}\''
        )
        assert request_object.to_curl() == expected_curl_command
