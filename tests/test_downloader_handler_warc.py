"""Tests for the WARC replay download handler."""

from __future__ import annotations

import gzip
import socket
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, Spider
from scrapy.core.downloader.handlers.warc import WarcDownloadHandler, WarcReplayError
from scrapy.exceptions import NotConfigured
from scrapy.http import TextResponse
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from pathlib import Path

REDIRECT_URL = "http://example.com/redirect"
PAGE_URL = "http://example.com/page"
OTHER_URL = "http://example.com/other"
CUTOFF = "2020-06-01T00:00:00Z"

OLD_PAGE_BODY = b"<html><head><title>Old page</title></head><body></body></html>"
PAGE_BODY_GBK = (
    "<html><head><title>离线页面</title></head>"
    '<body><a href="/other">下一页</a></body></html>'
).encode("gbk")
OTHER_BODY = b"<html><head><title>Other</title></head><body><p>other</p></body></html>"


def _http_response(
    status: int,
    reason: str,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
) -> bytes:
    lines = [f"HTTP/1.1 {status} {reason}".encode()]
    lines += [name + b": " + value for name, value in headers]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def _chunked(data: bytes) -> bytes:
    half = len(data) // 2
    return (
        b"".join(
            b"%x\r\n%s\r\n" % (len(part), part)
            for part in (data[:half], data[half:])
            if part
        )
        + b"0\r\n\r\n"
    )


def _warc_record(
    record_id: str,
    block: bytes,
    *,
    record_type: str = "response",
    target: str | None = None,
    date: str = "2020-01-01T00:00:00Z",
    refers_to: str | None = None,
) -> bytes:
    lines = [
        b"WARC/1.1",
        f"WARC-Record-ID: <urn:uuid:{record_id}>".encode(),
        f"WARC-Date: {date}".encode(),
        f"WARC-Type: {record_type}".encode(),
    ]
    if target is not None:
        lines.append(b"WARC-Target-URI: " + target.encode())
    if refers_to is not None:
        lines.append(f"WARC-Refers-To: <urn:uuid:{refers_to}>".encode())
    lines.append(b"Content-Type: application/http; msgtype=response")
    lines.append(f"Content-Length: {len(block)}".encode())
    return b"\r\n".join(lines) + b"\r\n\r\n" + block + b"\r\n\r\n"


def _site_records() -> list[bytes]:
    """A small archived site: two captures of one page, a redirect, a
    non-UTF-8 page sent chunked and gzipped, and a revisit record."""
    return [
        _warc_record(
            "r1",
            _http_response(301, "Moved Permanently", [(b"Location", b"/page")], b""),
            target=REDIRECT_URL,
        ),
        _warc_record(
            "r2",
            _http_response(
                200,
                "OK",
                [(b"Content-Type", b"text/html; charset=utf-8")],
                OLD_PAGE_BODY,
            ),
            target=PAGE_URL,
        ),
        _warc_record(
            "r3",
            _http_response(
                200,
                "OK",
                [
                    (b"Content-Type", b"text/html; charset=gbk"),
                    (b"Content-Encoding", b"gzip"),
                    (b"Transfer-Encoding", b"chunked"),
                    (b"Set-Cookie", b"a=1"),
                    (b"Set-Cookie", b"b=2"),
                    (b"X-Capture", b"new"),
                ],
                _chunked(gzip.compress(PAGE_BODY_GBK)),
            ),
            target=PAGE_URL,
            date="2020-06-01T00:00:00Z",
        ),
        _warc_record(
            "r4",
            _http_response(
                200,
                "OK",
                [
                    (b"Content-Type", b"text/html; charset=utf-8"),
                    (b"X-Capture", b"original"),
                ],
                OTHER_BODY,
            ),
            target=OTHER_URL,
        ),
        _warc_record(
            "r5",
            _http_response(
                200,
                "OK",
                [
                    (b"Content-Type", b"text/html; charset=utf-8"),
                    (b"X-Capture", b"revisit"),
                ],
                b"",
            ),
            record_type="revisit",
            target=OTHER_URL,
            date="2020-05-01T00:00:00Z",
            refers_to="r4",
        ),
    ]


def _write_archive(path: Path, records: list[bytes], *, compress: bool = False) -> Path:
    if compress:
        data = b"".join(gzip.compress(record) for record in records)
    else:
        data = b"".join(records)
    path.write_bytes(data)
    return path


def _build_handler(path: Path, cutoff: str | None = None) -> WarcDownloadHandler:
    settings: dict[str, str] = {"WARC_FILE": str(path)}
    if cutoff is not None:
        settings["WARC_CUTOFF"] = cutoff
    crawler = get_crawler(settings_dict=settings)
    return build_from_crawler(WarcDownloadHandler, crawler)


def _crawl_settings(path: Path) -> dict[str, Any]:
    handler = "scrapy.core.downloader.handlers.warc.WarcDownloadHandler"
    return {
        "WARC_FILE": str(path),
        "WARC_CUTOFF": CUTOFF,
        "DOWNLOAD_HANDLERS": {"http": handler, "https": handler},
    }


class ReplaySpider(Spider):
    name = "warc-replay-test"
    start_urls = [REDIRECT_URL]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pages = {}

    def parse(self, response):
        self.pages[response.url] = (
            response.status,
            response.css("title::text").get(),
            tuple(sorted(response.headers.getlist("Set-Cookie"))),
            response.headers.get("X-Capture"),
        )
        yield from response.follow_all(css="a", callback=self.parse)


class TestWarcDownloadHandler:
    def test_not_configured(self):
        with pytest.raises(NotConfigured):
            build_from_crawler(WarcDownloadHandler, get_crawler())

    @pytest.mark.parametrize("compress", [False, True])
    @coroutine_test
    async def test_replay_response(self, tmp_path, compress):
        path = _write_archive(
            tmp_path / "site.warc", _site_records(), compress=compress
        )
        handler = _build_handler(path, CUTOFF)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.url == PAGE_URL
        assert response.status == 200
        assert response.headers.getlist("Set-Cookie") == [b"a=1", b"b=2"]
        assert response.headers["X-Capture"] == b"new"

    @pytest.mark.parametrize("compress", [False, True])
    @coroutine_test
    async def test_replay_revisit(self, tmp_path, compress):
        path = _write_archive(
            tmp_path / "site.warc", _site_records(), compress=compress
        )
        handler = _build_handler(path, CUTOFF)
        response = await handler.download_request(Request(OTHER_URL))
        assert isinstance(response, TextResponse)
        assert response.status == 200
        # the headers come from the revisit record, the body from the
        # referenced response record
        assert response.headers["X-Capture"] == b"revisit"
        assert response.body == OTHER_BODY

    @pytest.mark.parametrize("compress", [False, True])
    @pytest.mark.parametrize(
        ("cutoff", "expected"),
        [
            (None, b"new"),
            ("2020-06-01T00:00:00Z", b"new"),
            ("2020-03-01T00:00:00Z", b"old"),
        ],
    )
    @coroutine_test
    async def test_cutoff_selection(self, tmp_path, compress, cutoff, expected):
        records = [
            _warc_record("r1", _http_response(200, "OK", [], b"old"), target=PAGE_URL),
            _warc_record(
                "r2",
                _http_response(200, "OK", [], b"new"),
                target=PAGE_URL,
                date="2020-06-01T00:00:00Z",
            ),
        ]
        path = _write_archive(tmp_path / "site.warc", records, compress=compress)
        handler = _build_handler(path, cutoff)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == expected

    @coroutine_test
    async def test_same_date_file_order(self, tmp_path):
        records = [
            _warc_record(
                "r1", _http_response(200, "OK", [], b"first"), target=PAGE_URL
            ),
            _warc_record(
                "r2", _http_response(200, "OK", [], b"second"), target=PAGE_URL
            ),
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path, CUTOFF)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == b"second"

    @coroutine_test
    async def test_no_capture(self, tmp_path):
        path = _write_archive(tmp_path / "site.warc", _site_records())
        handler = _build_handler(path, CUTOFF)
        with pytest.raises(WarcReplayError, match="No capture"):
            await handler.download_request(Request("http://example.com/unknown"))

    @coroutine_test
    async def test_no_capture_before_cutoff(self, tmp_path):
        path = _write_archive(tmp_path / "site.warc", _site_records())
        handler = _build_handler(path, "2019-01-01T00:00:00Z")
        with pytest.raises(WarcReplayError, match="No capture"):
            await handler.download_request(Request(PAGE_URL))

    @coroutine_test
    async def test_revisit_missing_payload(self, tmp_path):
        records = [
            _warc_record(
                "r1",
                _http_response(200, "OK", [], b""),
                record_type="revisit",
                target=PAGE_URL,
                refers_to="missing",
            )
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path, CUTOFF)
        with pytest.raises(WarcReplayError, match="not in the archive"):
            await handler.download_request(Request(PAGE_URL))

    @coroutine_test
    async def test_revisit_without_refers_to(self, tmp_path):
        records = [
            _warc_record(
                "r1",
                _http_response(200, "OK", [], b""),
                record_type="revisit",
                target=PAGE_URL,
            )
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path, CUTOFF)
        with pytest.raises(WarcReplayError, match="WARC-Refers-To"):
            await handler.download_request(Request(PAGE_URL))

    @coroutine_test
    async def test_revisit_cycle(self, tmp_path):
        records = [
            _warc_record(
                "r1",
                _http_response(200, "OK", [], b""),
                record_type="revisit",
                target=PAGE_URL,
                refers_to="r2",
            ),
            _warc_record(
                "r2",
                _http_response(200, "OK", [], b""),
                record_type="revisit",
                target=PAGE_URL,
                date="2020-02-01T00:00:00Z",
                refers_to="r1",
            ),
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path, CUTOFF)
        with pytest.raises(WarcReplayError, match="cycle"):
            await handler.download_request(Request(PAGE_URL))

    @pytest.mark.parametrize("compress", [False, True])
    def test_truncated_record(self, tmp_path, compress):
        record = _warc_record(
            "r1", _http_response(200, "OK", [], b"body"), target=PAGE_URL
        )
        data = gzip.compress(record)[:20] if compress else record[:-10]
        path = tmp_path / "site.warc"
        path.write_bytes(data)
        with pytest.raises(WarcReplayError, match="Truncated"):
            _build_handler(path)

    @coroutine_test
    async def test_encoding_layers(self, tmp_path):
        body_gz = gzip.compress(b"hello")
        records = [
            _warc_record(
                "r1",
                _http_response(
                    200,
                    "OK",
                    [
                        (b"Content-Type", b"text/html"),
                        (b"Content-Encoding", b"gzip"),
                        (b"Transfer-Encoding", b"chunked"),
                    ],
                    _chunked(body_gz),
                ),
                target=PAGE_URL,
            )
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path)
        response = await handler.download_request(Request(PAGE_URL))
        # the transfer encoding is undone by the handler, the content
        # encoding is left for the HTTP compression middleware
        assert response.body == body_gz
        assert response.headers["Transfer-Encoding"] == b"chunked"
        assert response.headers["Content-Encoding"] == b"gzip"

    @pytest.mark.parametrize("compress", [False, True])
    @coroutine_test
    async def test_unselected_records_are_not_read(self, tmp_path, compress):
        records = [
            _warc_record("r1", b"this is not an HTTP message", target=OTHER_URL),
            _warc_record("r2", _http_response(200, "OK", [], b"ok"), target=PAGE_URL),
        ]
        path = _write_archive(tmp_path / "site.warc", records, compress=compress)
        handler = _build_handler(path)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == b"ok"
        with pytest.raises(WarcReplayError, match="Malformed HTTP"):
            await handler.download_request(Request(OTHER_URL))

    def test_invalid_cutoff(self, tmp_path):
        path = _write_archive(tmp_path / "site.warc", _site_records())
        with pytest.raises(WarcReplayError, match="Invalid WARC-Date"):
            _build_handler(path, "not-a-date")

    @coroutine_test
    async def test_naive_cutoff(self, tmp_path):
        path = _write_archive(tmp_path / "site.warc", _site_records())
        handler = _build_handler(path, "2020-06-01T00:00:00")
        response = await handler.download_request(Request(PAGE_URL))
        assert response.status == 200

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            (b"garbage\r\n", "Expected a WARC record"),
            (b"WARC/1.1\r\nBad-Header-Line\r\n\r\n", "Malformed WARC header"),
            (
                b"WARC/1.1\r\nWARC-Record-ID: <urn:uuid:r1>\r\n\r\n",
                "Missing or invalid Content-Length",
            ),
            (
                b"WARC/1.1\r\nWARC-Record-ID: <urn:uuid:r1>\r\n",
                "Truncated WARC record",
            ),
            (gzip.compress(b"garbage"), "Malformed WARC record"),
            (gzip.compress(b"NOTWARC\r\n\r\n"), "Expected a WARC record"),
            (
                gzip.compress(
                    b"WARC/1.1\r\nWARC-Record-ID: <urn:uuid:r1>\r\n"
                    b"Content-Length: 100\r\n\r\nshort"
                ),
                "Truncated WARC record",
            ),
            (
                gzip.compress(_warc_record("r1", b"", target=PAGE_URL)) + b"garbage",
                "Expected a gzip member",
            ),
        ],
    )
    def test_malformed_archive(self, tmp_path, data, match):
        path = tmp_path / "site.warc"
        path.write_bytes(data)
        with pytest.raises(WarcReplayError, match=match):
            _build_handler(path)

    @coroutine_test
    async def test_gzip_trailing_whitespace(self, tmp_path):
        record = _warc_record(
            "r1", _http_response(200, "OK", [], b"ok"), target=PAGE_URL
        )
        path = tmp_path / "site.warc"
        path.write_bytes(gzip.compress(record) + b"\r\n")
        handler = _build_handler(path)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == b"ok"

    def test_record_missing_required_fields(self, tmp_path):
        path = tmp_path / "site.warc"
        path.write_bytes(
            b"WARC/1.1\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Type: response\r\n"
            b"WARC-Target-URI: http://example.com/page\r\n"
            b"Content-Length: 0\r\n\r\n\r\n\r\n"
        )
        with pytest.raises(WarcReplayError, match="WARC-Record-ID"):
            _build_handler(path)

    @coroutine_test
    async def test_non_capture_records_are_skipped(self, tmp_path):
        warcinfo = (
            b"WARC/1.1\r\n"
            b"WARC-Record-ID: <urn:uuid:info>\r\n"
            b"WARC-Date: 2020-01-01T00:00:00Z\r\n"
            b"WARC-Type: warcinfo\r\n"
            b"Content-Length: 4\r\n\r\n"
            b"info\r\n\r\n"
        )
        records = [
            warcinfo,
            _warc_record("r1", _http_response(200, "OK", [], b"ok"), target=PAGE_URL),
        ]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == b"ok"

    @pytest.mark.parametrize(
        ("block", "match"),
        [
            (b"NOTHTTP\r\n\r\nbody", "Malformed HTTP status line"),
            (
                b"HTTP/1.1 200 OK\r\nbadheader\r\n\r\nbody",
                "Malformed HTTP header line",
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5",
                "Truncated chunked body",
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
                "Malformed chunk size",
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n",
                "Truncated chunked body",
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc",
                "Truncated chunked body",
            ),
            (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: deflate\r\n\r\nbody",
                "Unsupported Transfer-Encoding",
            ),
        ],
    )
    @coroutine_test
    async def test_malformed_http_message(self, tmp_path, block, match):
        records = [_warc_record("r1", block, target=PAGE_URL)]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path)
        with pytest.raises(WarcReplayError, match=match):
            await handler.download_request(Request(PAGE_URL))

    @coroutine_test
    async def test_chunked_with_trailers(self, tmp_path):
        block = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Trailer: 1\r\n\r\n"
        )
        records = [_warc_record("r1", block, target=PAGE_URL)]
        path = _write_archive(tmp_path / "site.warc", records)
        handler = _build_handler(path)
        response = await handler.download_request(Request(PAGE_URL))
        assert response.body == b"abc"


class TestOfflineCrawl:
    @pytest.mark.parametrize("compress", [False, True])
    @coroutine_test
    async def test_offline_replay_is_repeatable(self, tmp_path, monkeypatch, compress):
        path = _write_archive(
            tmp_path / "site.warc", _site_records(), compress=compress
        )
        connections = []

        def forbidden_connect(sock, address):
            connections.append(address)
            raise AssertionError(f"Unexpected network connection to {address}")

        monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", forbidden_connect)
        results = []
        for _ in range(2):
            crawler = get_crawler(ReplaySpider, _crawl_settings(path))
            await crawler.crawl_async()
            assert isinstance(crawler.spider, ReplaySpider)
            results.append(crawler.spider.pages)
        assert connections == []
        assert results[0] == results[1]
        assert results[0] == {
            PAGE_URL: (200, "离线页面", (b"a=1", b"b=2"), b"new"),
            OTHER_URL: (200, "Other", (), b"revisit"),
        }
