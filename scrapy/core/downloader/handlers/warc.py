from __future__ import annotations

import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NamedTuple

from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.exceptions import NotConfigured
from scrapy.http import Headers, Response
from scrapy.responsetypes import responsetypes
from scrapy.utils.asyncio import run_in_thread

if TYPE_CHECKING:
    from scrapy import Request
    from scrapy.crawler import Crawler


class WarcReplayError(Exception):
    """A response could not be rebuilt from the WARC archive."""


_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_READ_CHUNK_SIZE = 65536


class _RecordInfo(NamedTuple):
    """Location and identity of a single WARC record."""

    offset: int  # start of the record (of its gzip member, if compressed)
    seq: int  # position of the record in the file
    record_id: str
    record_type: str
    target_uri: str
    date: datetime
    refers_to: str | None


def _parse_warc_date(value: str, context: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise WarcReplayError(f"Invalid WARC-Date {value!r} in {context}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_warc_headers(lines: list[bytes], context: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(b":")
        if not sep:
            raise WarcReplayError(f"Malformed WARC header line {line!r} in {context}")
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode(
            "latin-1"
        )
    return headers


def _content_length(headers: dict[str, str], context: str) -> int:
    value = headers.get("content-length", "")
    if not value.isdigit():
        raise WarcReplayError(f"Missing or invalid Content-Length in {context}")
    return int(value)


def _read_header_fields(fp: BinaryIO, context: str) -> dict[str, str]:
    """Read WARC header fields from *fp*, positioned after the version line."""
    lines: list[bytes] = []
    while True:
        line = fp.readline()
        if not line:
            raise WarcReplayError(f"Truncated WARC record in {context}")
        if not line.strip():
            return _parse_warc_headers(lines, context)
        lines.append(line)


def _split_record(data: bytes, context: str) -> tuple[dict[str, str], bytes]:
    """Split the decompressed bytes of a WARC record into headers and block."""
    header_section, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        raise WarcReplayError(f"Malformed WARC record in {context}")
    lines = header_section.split(b"\r\n")
    if not lines[0].startswith(b"WARC/"):
        raise WarcReplayError(f"Expected a WARC record in {context}, got {lines[0]!r}")
    headers = _parse_warc_headers(lines[1:], context)
    content_length = _content_length(headers, context)
    block, trailing = rest[:content_length], rest[content_length:]
    if len(block) != content_length or trailing.strip():
        raise WarcReplayError(f"Truncated WARC record in {context}")
    return headers, block


def _read_gzip_member(fp: BinaryIO, offset: int, path: Path) -> tuple[bytes, int]:
    """Decompress the gzip member starting at *offset*.

    Return the decompressed data and the compressed size of the member.
    """
    fp.seek(offset)
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    chunks: list[bytes] = []
    read = 0
    while True:
        chunk = fp.read(_READ_CHUNK_SIZE)
        if not chunk:
            raise WarcReplayError(
                f"Truncated gzip-compressed WARC record at offset {offset} in {path}"
            )
        read += len(chunk)
        chunks.append(decompressor.decompress(chunk))
        if decompressor.eof:
            return b"".join(chunks), read - len(decompressor.unused_data)


def _parse_http_response(
    block: bytes, context: str
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Split a stored HTTP response into status, headers and entity body."""
    head, sep, body = block.partition(b"\r\n\r\n")
    if not sep:
        raise WarcReplayError(f"Malformed HTTP response in {context}")
    status_line, *header_lines = head.split(b"\r\n")
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise WarcReplayError(
            f"Malformed HTTP status line {status_line!r} in {context}"
        )
    headers: list[tuple[bytes, bytes]] = []
    for line in header_lines:
        name, sep, value = line.partition(b":")
        if not sep:
            raise WarcReplayError(f"Malformed HTTP header line {line!r} in {context}")
        headers.append((name.strip(), value.strip()))
    return int(parts[1]), headers, body


def _dechunk(body: bytes, context: str) -> bytes:
    """Decode an HTTP body sent with chunked transfer encoding."""
    decoded = bytearray()
    pos = 0
    while True:
        end = body.find(b"\r\n", pos)
        if end == -1:
            raise WarcReplayError(f"Truncated chunked body in {context}")
        size_text = body[pos:end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise WarcReplayError(
                f"Malformed chunk size {size_text!r} in {context}"
            ) from None
        pos = end + 2
        if size == 0:
            # The optional trailer part ends with an empty line; its fields
            # are not needed to rebuild the response.
            if body[pos : pos + 2] == b"\r\n" or b"\r\n\r\n" in body[pos:]:
                return bytes(decoded)
            raise WarcReplayError(f"Truncated chunked body in {context}")
        if pos + size + 2 > len(body) or body[pos + size : pos + size + 2] != b"\r\n":
            raise WarcReplayError(f"Truncated chunked body in {context}")
        decoded += body[pos : pos + size]
        pos += size + 2


def _decode_transfer_encoding(
    headers: list[tuple[bytes, bytes]], body: bytes, context: str
) -> bytes:
    """Undo the HTTP transfer encoding of a stored response body.

    Content encodings (e.g. gzip) are left untouched: they are handled by
    the HTTP compression downloader middleware, like for any other response.
    """
    encodings = [
        value.strip().lower()
        for name, value in headers
        if name.lower() == b"transfer-encoding"
    ]
    if not encodings:
        return body
    if encodings != [b"chunked"]:
        raise WarcReplayError(f"Unsupported Transfer-Encoding {encodings} in {context}")
    return _dechunk(body, context)


class _WarcArchive:
    """Offset index of the captures stored in a WARC 1.1 file.

    Indexing reads record headers (decompressing and discarding record
    bodies of per-record gzip files, which cannot be skipped otherwise) but
    never keeps response bodies in memory: the content of a record is only
    read when that record is selected for replay.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.gzipped = False
        self._captures: dict[str, list[_RecordInfo]] = {}
        self._records: dict[str, _RecordInfo] = {}
        self._index()

    def select(self, url: str, cutoff: datetime | None) -> _RecordInfo:
        """Return the most recent capture of *url* not later than *cutoff*."""
        captures = self._captures.get(url)
        if not captures:
            raise WarcReplayError(f"No capture of {url} found in {self.path}")
        eligible = [c for c in captures if cutoff is None or c.date <= cutoff]
        if not eligible:
            assert cutoff is not None
            raise WarcReplayError(
                f"No capture of {url} at or before "
                f"{cutoff.isoformat()} found in {self.path}"
            )
        return max(eligible, key=lambda c: (c.date, c.seq))

    def load_http_response(
        self, info: _RecordInfo
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        """Rebuild the HTTP response captured in the record *info*."""
        return self._load(info, set())

    def _load(
        self, info: _RecordInfo, seen: set[str]
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        if info.record_id in seen:
            raise WarcReplayError(
                f"Revisit records in {self.path} refer to each other in a "
                f"cycle involving record {info.record_id}"
            )
        seen.add(info.record_id)
        context = f"record {info.record_id} in {self.path}"
        _, block = self._read_record(info)
        status, headers, body = _parse_http_response(block, context)
        if info.record_type == "revisit":
            # A revisit record carries the HTTP headers of the capture but
            # not its payload, which is identical to the one of the record
            # it refers to.
            if not info.refers_to:
                raise WarcReplayError(f"Revisit {context} has no WARC-Refers-To header")
            target = self._records.get(info.refers_to)
            if target is None:
                raise WarcReplayError(
                    f"Revisit {context} refers to record {info.refers_to} "
                    f"which is not in the archive"
                )
            _, _, body = self._load(target, seen)
        else:
            body = _decode_transfer_encoding(headers, body, context)
        return status, headers, body

    def _index(self) -> None:
        with self.path.open("rb") as fp:
            fp.seek(0, 2)
            size = fp.tell()
            fp.seek(0)
            self.gzipped = fp.read(2) == _GZIP_MAGIC
            fp.seek(0)
            if self.gzipped:
                self._index_gzip(fp, size)
            else:
                self._index_plain(fp, size)

    def _index_plain(self, fp: BinaryIO, size: int) -> None:
        seq = 0
        while True:
            offset = fp.tell()
            line = fp.readline()
            if not line:
                return
            if not line.strip():
                continue
            context = f"record at offset {offset} in {self.path}"
            if not line.startswith(b"WARC/"):
                raise WarcReplayError(f"Expected a WARC record in {context}")
            headers = _read_header_fields(fp, context)
            end = fp.tell() + _content_length(headers, context)
            if end > size:
                raise WarcReplayError(f"Truncated WARC record in {context}")
            self._add_record(headers, offset, seq)
            seq += 1
            fp.seek(end)

    def _index_gzip(self, fp: BinaryIO, size: int) -> None:
        offset = 0
        seq = 0
        while offset < size:
            fp.seek(offset)
            if fp.read(2) != _GZIP_MAGIC:
                fp.seek(offset)
                if fp.read().strip():
                    raise WarcReplayError(
                        f"Expected a gzip member at offset {offset} in {self.path}"
                    )
                return
            data, consumed = _read_gzip_member(fp, offset, self.path)
            headers, _ = _split_record(
                data, f"gzip member at offset {offset} in {self.path}"
            )
            self._add_record(headers, offset, seq)
            offset += consumed
            seq += 1

    def _add_record(self, headers: dict[str, str], offset: int, seq: int) -> None:
        record_type = headers.get("warc-type", "")
        if record_type not in ("response", "revisit"):
            return
        context = f"record at offset {offset} in {self.path}"
        record_id = headers.get("warc-record-id")
        target_uri = headers.get("warc-target-uri")
        date = headers.get("warc-date")
        if record_id is None or target_uri is None or date is None:
            raise WarcReplayError(
                "A WARC-Record-ID, a WARC-Target-URI and a WARC-Date are "
                f"required in {context}"
            )
        info = _RecordInfo(
            offset=offset,
            seq=seq,
            record_id=record_id,
            record_type=record_type,
            target_uri=target_uri,
            date=_parse_warc_date(date, context),
            refers_to=headers.get("warc-refers-to"),
        )
        self._records[info.record_id] = info
        self._captures.setdefault(info.target_uri, []).append(info)

    def _read_record(self, info: _RecordInfo) -> tuple[dict[str, str], bytes]:
        context = f"record at offset {info.offset} in {self.path}"
        with self.path.open("rb") as fp:
            if self.gzipped:
                data, _ = _read_gzip_member(fp, info.offset, self.path)
                return _split_record(data, context)
            fp.seek(info.offset)
            version = fp.readline()
            # The index already validated the record at this offset.
            if not version.startswith(b"WARC/"):  # pragma: no cover
                raise WarcReplayError(f"Expected a WARC record in {context}")
            headers = _read_header_fields(fp, context)
            content_length = _content_length(headers, context)
            block = fp.read(content_length)
            if len(block) != content_length:  # pragma: no cover
                raise WarcReplayError(f"Truncated WARC record in {context}")
            return headers, block


class WarcDownloadHandler(BaseDownloadHandler):
    """Rebuild HTTP responses from a local WARC 1.1 file.

    .. versionadded:: VERSION

    See :ref:`warc-replay`.
    """

    lazy = True

    def __init__(self, crawler: Crawler):
        super().__init__(crawler)
        path = crawler.settings.get("WARC_FILE")
        if not path:
            raise NotConfigured("WARC_FILE is not set")
        self._archive = _WarcArchive(path)
        cutoff = crawler.settings.get("WARC_CUTOFF")
        self._cutoff: datetime | None = (
            _parse_warc_date(cutoff, "the WARC_CUTOFF setting") if cutoff else None
        )

    async def download_request(self, request: Request) -> Response:
        return await run_in_thread(self._replay, request)

    def _replay(self, request: Request) -> Response:
        info = self._archive.select(request.url, self._cutoff)
        status, headers, body = self._archive.load_http_response(info)
        response_headers = Headers(headers)
        respcls = responsetypes.from_args(headers=response_headers, body=body)
        return respcls(
            url=request.url, status=status, headers=response_headers, body=body
        )
