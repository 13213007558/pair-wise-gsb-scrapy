"""Tests for the WARC download handler."""

from __future__ import annotations

import gzip
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, Spider
from scrapy.core.downloader.handlers._warc import (
    WARCDownloadHandler,
    WARCPayloadNotFoundError,
    WARCRecordNotFoundError,
    WARCReferenceLoopError,
    WARCTruncatedRecordError,
)
from scrapy.exceptions import NotConfigured
from scrapy.http import Response, TextResponse
from scrapy.linkextractors import LinkExtractor
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

REPLAY_TIME = "2020-03-01T00:00:00Z"

HOME_BODY = (
    b"<html><body>old home <a href='/page2'>p2</a> <a href='/redirect'>r</a>"
    b" <a href='/gzipped'>g</a></body></html>"
)
PAGE2_BODY = "<html><body>caf\u00e9 <a href='/revisit'>rev</a></body></html>".encode(
    "iso-8859-1"
)
TARGET_BODY = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
GZIPPED_BODY = gzip.compress(b"<html><body>compressed page</body></html>")


def _http_response(
    status: int = 200,
    reason: bytes = b"OK",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> bytes:
    lines = [b"HTTP/1.1 %d %s" % (status, reason)]
    lines += [b"%s: %s" % header for header in headers or []]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def _warc_record(
    record_type: bytes,
    url: bytes,
    date: bytes,
    record_id: bytes,
    payload: bytes,
    refers_to: bytes | None = None,
) -> bytes:
    fields = [
        b"WARC/1.1",
        b"WARC-Type: " + record_type,
        b"WARC-Target-URI: " + url,
        b"WARC-Date: " + date,
        b"WARC-Record-ID: <urn:uuid:" + record_id + b">",
        b"Content-Type: application/http; msgtype=response",
        b"Content-Length: " + str(len(payload)).encode(),
    ]
    if refers_to is not None:
        fields.append(b"WARC-Refers-To: <urn:uuid:" + refers_to + b">")
    return b"\r\n".join(fields) + b"\r\n\r\n" + payload + b"\r\n\r\n"


def _main_records() -> list[bytes]:
    return [
        _warc_record(
            b"response",
            b"http://example.com/",
            b"2020-01-01T00:00:00Z",
            b"r1",
            _http_response(headers=[(b"Content-Type", b"text/html")], body=HOME_BODY),
        ),
        _warc_record(
            b"response",
            b"http://example.com/",
            b"2020-06-01T00:00:00Z",
            b"r2",
            _http_response(
                headers=[(b"Content-Type", b"text/html")],
                body=b"<html><body>new home</body></html>",
            ),
        ),
        _warc_record(
            b"response",
            b"http://example.com/page2",
            b"2020-01-01T00:00:00Z",
            b"r3",
            _http_response(
                headers=[(b"Content-Type", b"text/html; charset=iso-8859-1")],
                body=PAGE2_BODY,
            ),
        ),
        _warc_record(
            b"response",
            b"http://example.com/redirect",
            b"2020-01-01T00:00:00Z",
            b"r4",
            _http_response(
                301, b"Moved", [(b"Location", b"http://example.com/target")]
            ),
        ),
        _warc_record(
            b"response",
            b"http://example.com/target",
            b"2020-01-01T00:00:00Z",
            b"r5",
            _http_response(
                headers=[
                    (b"Content-Type", b"text/html"),
                    (b"Transfer-Encoding", b"chunked"),
                    (b"Set-Cookie", b"a=1"),
                    (b"Set-Cookie", b"b=2"),
                ],
                body=TARGET_BODY,
            ),
        ),
        _warc_record(
            b"revisit",
            b"http://example.com/revisit",
            b"2020-02-01T00:00:00Z",
            b"r6",
            b"",
            refers_to=b"r3",
        ),
        _warc_record(
            b"response",
            b"http://example.com/gzipped",
            b"2020-01-01T00:00:00Z",
            b"r7",
            _http_response(
                headers=[
                    (b"Content-Type", b"text/html"),
                    (b"Content-Encoding", b"gzip"),
                ],
                body=GZIPPED_BODY,
            ),
        ),
    ]


def _write_archive(path: Path, records: list[bytes], gzipped: bool) -> Path:
    if gzipped:
        path.write_bytes(b"".join(gzip.compress(record) for record in records))
    else:
        path.write_bytes(b"".join(records))
    return path


@pytest.fixture(params=[False, True], ids=["plain", "gzip"])
def archive_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    gzipped = request.param
    name = "archive.warc.gz" if gzipped else "archive.warc"
    return _write_archive(tmp_path / name, _main_records(), gzipped)


def _settings(archive_path: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy.core.downloader.handlers._warc.WARCDownloadHandler",
            "https": "scrapy.core.downloader.handlers._warc.WARCDownloadHandler",
        },
        "WARC_FILE": str(archive_path),
        "WARC_REPLAY_TIME": REPLAY_TIME,
        **overrides,
    }


def _get_handler(crawler: Crawler) -> WARCDownloadHandler:
    return build_from_crawler(WARCDownloadHandler, crawler)


class ReplaySpider(Spider):
    name = "warc_replay"
    start_urls = ["http://example.com/"]

    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, int, str]] = []

    def parse(self, response: Response) -> Any:
        assert isinstance(response, TextResponse)
        self.items.append((response.url, response.status, response.text))
        for link in LinkExtractor().extract_links(response):
            yield Request(link.url, callback=self.parse)


@pytest.fixture
def network_connections(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    attempts: list[Any] = []

    def _connect(self: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise OSError(f"network access is disabled: {address}")

    def _connect_ex(self: socket.socket, address: Any) -> int:
        attempts.append(address)
        raise OSError(f"network access is disabled: {address}")

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    return attempts


class TestWARCReplay:
    @coroutine_test
    async def test_offline_replay_is_deterministic(
        self, archive_path: Path, network_connections: list[Any]
    ) -> None:
        runs = []
        for _ in range(2):
            crawler = get_crawler(ReplaySpider, _settings(archive_path))
            await crawler.crawl_async()
            assert isinstance(crawler.spider, ReplaySpider)
            runs.append(crawler.spider.items)
        assert runs[0] == runs[1]
        assert network_connections == []

        by_url = dict((url, (status, text)) for url, status, text in runs[0])
        assert set(by_url) == {
            "http://example.com/",
            "http://example.com/page2",
            "http://example.com/target",
            "http://example.com/revisit",
            "http://example.com/gzipped",
        }
        assert "old home" in by_url["http://example.com/"][1]
        assert "new home" not in by_url["http://example.com/"][1]
        assert "caf\u00e9" in by_url["http://example.com/page2"][1]
        assert by_url["http://example.com/target"] == (200, "hello world")
        assert "caf\u00e9" in by_url["http://example.com/revisit"][1]
        assert "compressed page" in by_url["http://example.com/gzipped"][1]

    @coroutine_test
    async def test_latest_capture_without_replay_time(
        self, archive_path: Path, network_connections: list[Any]
    ) -> None:
        crawler = get_crawler(
            ReplaySpider, _settings(archive_path, WARC_REPLAY_TIME=None)
        )
        await crawler.crawl_async()
        assert isinstance(crawler.spider, ReplaySpider)
        texts = [text for _, _, text in crawler.spider.items]
        assert any("new home" in text for text in texts)
        assert network_connections == []

    @coroutine_test
    async def test_response_details(self, archive_path: Path) -> None:
        crawler = get_crawler(settings_dict=_settings(archive_path))
        handler = _get_handler(crawler)
        response = await handler.download_request(Request("http://example.com/target"))
        assert response.status == 200
        assert response.body == b"hello world"
        assert response.headers.getlist(b"Set-Cookie") == [b"a=1", b"b=2"]

    def test_not_configured(self) -> None:
        crawler = get_crawler()
        with pytest.raises(NotConfigured):
            _get_handler(crawler)

    @coroutine_test
    async def test_no_matching_capture(self, archive_path: Path) -> None:
        crawler = get_crawler(settings_dict=_settings(archive_path))
        handler = _get_handler(crawler)
        with pytest.raises(WARCRecordNotFoundError):
            await handler.download_request(Request("http://example.com/missing"))

    @coroutine_test
    async def test_capture_after_replay_time(self, archive_path: Path) -> None:
        settings = _settings(archive_path, WARC_REPLAY_TIME="2019-01-01T00:00:00Z")
        crawler = get_crawler(settings_dict=settings)
        handler = _get_handler(crawler)
        with pytest.raises(WARCRecordNotFoundError):
            await handler.download_request(Request("http://example.com/"))

    @coroutine_test
    async def test_unselected_records_are_not_read(self, tmp_path: Path) -> None:
        # A truncated record that is never selected does not fail the replay,
        # because record payloads are only read when their record is selected.
        good = _warc_record(
            b"response",
            b"http://example.com/",
            b"2020-01-01T00:00:00Z",
            b"r1",
            _http_response(body=b"fine"),
        )
        truncated = _warc_record(
            b"response",
            b"http://example.com/truncated",
            b"2020-01-01T00:00:00Z",
            b"r2",
            _http_response(body=b"this body is cut short"),
        )[:-10]
        path = _write_archive(tmp_path / "archive.warc", [good, truncated], False)
        crawler = get_crawler(settings_dict=_settings(path))
        handler = _get_handler(crawler)
        response = await handler.download_request(Request("http://example.com/"))
        assert response.body == b"fine"

    @pytest.mark.parametrize("gzipped", [False, True])
    @coroutine_test
    async def test_truncated_record(self, tmp_path: Path, gzipped: bool) -> None:
        record = _warc_record(
            b"response",
            b"http://example.com/",
            b"2020-01-01T00:00:00Z",
            b"r1",
            _http_response(body=b"this body is cut short"),
        )
        if gzipped:
            data = gzip.compress(record)[:-5]
        else:
            data = record[:-10]
        name = "archive.warc.gz" if gzipped else "archive.warc"
        path = tmp_path / name
        path.write_bytes(data)
        if gzipped:
            crawler = get_crawler(settings_dict=_settings(path))
            with pytest.raises(WARCTruncatedRecordError):
                _get_handler(crawler)
        else:
            crawler = get_crawler(settings_dict=_settings(path))
            handler = _get_handler(crawler)
            with pytest.raises(WARCTruncatedRecordError):
                await handler.download_request(Request("http://example.com/"))

    @coroutine_test
    async def test_missing_revisit_payload(self, tmp_path: Path) -> None:
        record = _warc_record(
            b"revisit",
            b"http://example.com/",
            b"2020-01-01T00:00:00Z",
            b"r1",
            b"",
            refers_to=b"nonexistent",
        )
        path = _write_archive(tmp_path / "archive.warc", [record], False)
        crawler = get_crawler(settings_dict=_settings(path))
        handler = _get_handler(crawler)
        with pytest.raises(WARCPayloadNotFoundError):
            await handler.download_request(Request("http://example.com/"))

    @coroutine_test
    async def test_revisit_reference_loop(self, tmp_path: Path) -> None:
        records = [
            _warc_record(
                b"revisit",
                b"http://example.com/a",
                b"2020-01-01T00:00:00Z",
                b"r1",
                b"",
                refers_to=b"r2",
            ),
            _warc_record(
                b"revisit",
                b"http://example.com/b",
                b"2020-01-01T00:00:00Z",
                b"r2",
                b"",
                refers_to=b"r1",
            ),
        ]
        path = _write_archive(tmp_path / "archive.warc", records, False)
        crawler = get_crawler(settings_dict=_settings(path))
        handler = _get_handler(crawler)
        with pytest.raises(WARCReferenceLoopError):
            await handler.download_request(Request("http://example.com/a"))
