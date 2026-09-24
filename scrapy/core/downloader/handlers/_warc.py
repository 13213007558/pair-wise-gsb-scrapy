from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.exceptions import NotConfigured
from scrapy.http import Headers
from scrapy.utils._download_handlers import make_response
from scrapy.utils.asyncio import run_in_thread

if TYPE_CHECKING:
    from scrapy import Request
    from scrapy.crawler import Crawler
    from scrapy.http import Response


_CHUNK_SIZE = 65536

_GZIP_MAGIC = b"\x1f\x8b"


class WARCError(Exception):
    """Base class for errors raised when replaying a WARC file."""


class WARCRecordNotFoundError(WARCError):
    """No record in the WARC file matches the requested URL and replay time."""


class WARCTruncatedRecordError(WARCError):
    """A WARC record ends before its declared content length."""


class WARCPayloadNotFoundError(WARCError):
    """A revisit record references a record that is not in the WARC file."""


class WARCReferenceLoopError(WARCError):
    """Revisit records reference each other in a loop."""


def _parse_datetime(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise WARCError(f"invalid WARC datetime: {value!r}") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _parse_warc_headers(block: bytes, offset: int) -> tuple[dict[str, str], int]:
    """Parse the version line and named fields of a WARC record.

    Return the fields and the length of the record up to and including the
    blank line that separates them from the content block.
    """
    head, sep, _ = block.partition(b"\r\n\r\n")
    if not sep:
        raise WARCError(f"WARC record at offset {offset} has no header terminator")
    lines = head.split(b"\r\n")
    if not lines[0].startswith(b"WARC/"):
        raise WARCError(f"expected a WARC version line at offset {offset}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon:
            raise WARCError(f"malformed WARC header line at offset {offset}: {line!r}")
        headers[name.strip().lower().decode("ascii")] = value.strip().decode("utf-8")
    return headers, len(head) + len(sep)


def _normalize_record_id(value: str) -> str:
    return value.strip().strip("<>")


@dataclass
class _Record:
    """Location of a WARC response or revisit record inside the file."""

    record_id: str
    record_type: str
    url: str
    date: datetime
    refers_to: str | None
    offset: int
    header_length: int
    payload_length: int
    order: int


def _read_gzip_member(fileobj: BinaryIO, offset: int) -> tuple[bytes, int]:
    """Decompress the gzip member starting at *offset*.

    Return the decompressed data and the offset right after the member.
    """
    fileobj.seek(offset)
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    chunks = []
    while True:
        chunk = fileobj.read(_CHUNK_SIZE)
        if not chunk:
            if not decomp.eof:
                raise WARCTruncatedRecordError(
                    f"gzip stream at offset {offset} ends abruptly"
                )
            break
        try:
            chunks.append(decomp.decompress(chunk))
        except zlib.error as exc:
            raise WARCError(f"invalid gzip data at offset {offset}: {exc}") from exc
        if decomp.eof:
            break
    end_offset = fileobj.tell() - len(decomp.unused_data)
    return b"".join(chunks), end_offset


class _WARCArchive:
    """Offset index of the response and revisit records of a WARC 1.1 file.

    Indexing reads record headers only; content blocks are read from disk
    when their record is selected for replay.
    """

    def __init__(self, path: Path):
        self._path = path
        self._records: list[_Record] = []
        self._by_url: dict[str, list[int]] = {}
        self._by_id: dict[str, int] = {}
        with path.open("rb") as fileobj:
            self._gzipped = fileobj.read(2) == _GZIP_MAGIC
            fileobj.seek(0, 2)
            self._size = fileobj.tell()
            fileobj.seek(0)
            if self._gzipped:
                self._index_gzip(fileobj)
            else:
                self._index_plain(fileobj)

    def _add_record(self, block: bytes, offset: int) -> _Record:
        headers, header_length = _parse_warc_headers(block, offset)
        try:
            payload_length = int(headers["content-length"])
        except KeyError:
            raise WARCError(
                f"WARC record at offset {offset} has no Content-Length header"
            ) from None
        except ValueError:
            raise WARCError(
                f"WARC record at offset {offset} has an invalid Content-Length"
                f" header: {headers['content-length']!r}"
            ) from None
        record = _Record(
            record_id=_normalize_record_id(headers.get("warc-record-id", "")),
            record_type=headers.get("warc-type", ""),
            url=headers.get("warc-target-uri", ""),
            date=_parse_datetime(headers.get("warc-date", "")),
            refers_to=_normalize_record_id(headers.get("warc-refers-to", "")) or None,
            offset=offset,
            header_length=header_length,
            payload_length=payload_length,
            order=len(self._records),
        )
        index = len(self._records)
        self._records.append(record)
        if record.record_type in ("response", "revisit"):
            self._by_url.setdefault(record.url, []).append(index)
            self._by_id[record.record_id] = index
        return record

    def _index_plain(self, fileobj: BinaryIO) -> None:
        while True:
            offset = self._skip_blank_lines(fileobj)
            if offset is None:
                return
            block = b""
            while True:
                line = fileobj.readline()
                if not line:
                    raise WARCTruncatedRecordError(
                        f"WARC record at offset {offset} ends inside its headers"
                    )
                block += line
                if line in (b"\r\n", b"\n"):
                    break
            record = self._add_record(block, offset)
            fileobj.seek(offset + record.header_length + record.payload_length)

    @staticmethod
    def _skip_blank_lines(fileobj: BinaryIO) -> int | None:
        while True:
            position = fileobj.tell()
            line = fileobj.readline()
            if not line:
                return None
            if line.strip():
                fileobj.seek(position)
                return position

    def _index_gzip(self, fileobj: BinaryIO) -> None:
        offset = 0
        while offset < self._size:
            fileobj.seek(offset)
            if fileobj.read(2) != _GZIP_MAGIC:
                fileobj.seek(offset)
                if not fileobj.read().strip():
                    return
                raise WARCError(f"expected a gzip member at offset {offset}")
            data, next_offset = _read_gzip_member(fileobj, offset)
            self._add_record(data, offset)
            offset = next_offset

    def find(self, url: str, cutoff: datetime | None) -> _Record:
        """Return the newest record for *url* captured no later than *cutoff*.

        Records captured at the same time are ordered by their position in
        the file.
        """
        candidates = self._by_url.get(url, [])
        if cutoff is not None:
            candidates = [i for i in candidates if self._records[i].date <= cutoff]
        if not candidates:
            message = f"no WARC record for {url}"
            if cutoff is not None:
                message += f" captured before {cutoff.isoformat()}"
            raise WARCRecordNotFoundError(message)
        index = max(
            candidates,
            key=lambda i: (self._records[i].date, self._records[i].order),
        )
        return self._records[index]

    def read_payload(self, record: _Record) -> bytes:
        """Return the content block of *record*, resolving revisit chains."""
        seen = set()
        while record.record_type == "revisit":
            if record.record_id in seen:
                raise WARCReferenceLoopError(
                    f"revisit records form a reference loop at {record.record_id}"
                )
            seen.add(record.record_id)
            target = self._by_id.get(record.refers_to or "")
            if target is None:
                raise WARCPayloadNotFoundError(
                    f"revisit record {record.record_id} references missing record"
                    f" {record.refers_to}"
                )
            record = self._records[target]
        return self._read_content(record)

    def _read_content(self, record: _Record) -> bytes:
        with self._path.open("rb") as fileobj:
            if self._gzipped:
                data, _ = _read_gzip_member(fileobj, record.offset)
                content = data[
                    record.header_length : record.header_length + record.payload_length
                ]
            else:
                fileobj.seek(record.offset + record.header_length)
                content = fileobj.read(record.payload_length)
        if len(content) < record.payload_length:
            raise WARCTruncatedRecordError(
                f"WARC record {record.record_id} declares {record.payload_length}"
                f" content bytes but only {len(content)} are available"
            )
        return content


def _decode_chunked(body: bytes) -> bytes:
    result = bytearray()
    position = 0
    while True:
        end = body.find(b"\r\n", position)
        if end == -1:
            raise WARCTruncatedRecordError("chunked body ends mid-chunk")
        size_text = body[position:end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise WARCError(f"invalid chunk size: {size_text!r}") from None
        position = end + 2
        if size == 0:
            return bytes(result)
        chunk = body[position : position + size]
        if len(chunk) < size:
            raise WARCTruncatedRecordError("chunked body ends mid-chunk")
        result += chunk
        position += size + 2


def _build_response(request: Request, payload: bytes) -> Response:
    head, sep, body = payload.partition(b"\r\n\r\n")
    if not sep:
        raise WARCError("WARC record payload is not a complete HTTP response")
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise WARCError(f"invalid HTTP status line: {lines[0]!r}")
    status = int(status_parts[1])
    header_list = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if colon:
            header_list.append((name.strip(), value.strip()))
    headers = Headers(header_list)
    # The transfer encoding is a property of the stored HTTP message, so it
    # is decoded here. The content encoding is a property of the response
    # itself and is left for HttpCompressionMiddleware to decode, as with
    # any other download handler.
    transfer_encoding = b",".join(headers.getlist(b"Transfer-Encoding")).lower()
    if b"chunked" in transfer_encoding:
        body = _decode_chunked(body)
    return make_response(request.url, status, headers, body)


class WARCDownloadHandler(BaseDownloadHandler):
    """Download handler that replays HTTP responses from a WARC 1.1 file.

    .. versionadded:: VERSION

    The file is read from the :setting:`WARC_FILE` setting and only
    captures not newer than :setting:`WARC_REPLAY_TIME` are considered.
    Uncompressed files and files with one gzip member per record are
    supported, as are revisit records that reference the payload of another
    record.
    """

    def __init__(self, crawler: Crawler):
        super().__init__(crawler)
        warc_file = crawler.settings.get("WARC_FILE")
        if not warc_file:
            raise NotConfigured("WARC_FILE is not set")
        replay_time = crawler.settings.get("WARC_REPLAY_TIME")
        self._cutoff = _parse_datetime(replay_time) if replay_time else None
        self._archive = _WARCArchive(Path(warc_file))

    async def download_request(self, request: Request) -> Response:
        record = self._archive.find(request.url, self._cutoff)
        payload = await run_in_thread(self._archive.read_payload, record)
        return _build_response(request, payload)
