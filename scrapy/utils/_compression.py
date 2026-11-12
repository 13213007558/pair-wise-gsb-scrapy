from __future__ import annotations

import struct
import sys
import zlib
from gzip import GzipFile
from io import BytesIO
from typing import TYPE_CHECKING

import brotli

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

if TYPE_CHECKING:
    from scrapy.http import Response


_CHUNK_SIZE = 65536  # 64 KiB

_DECOMPRESSION_ERRORS: tuple[type[Exception], ...] = (
    EOFError,
    OSError,
    brotli.error,
    struct.error,
    zlib.error,
    zstd.ZstdError,
)


class _DecompressionMaxSizeExceeded(ValueError):
    def __init__(self, decompressed_size: int, max_size: int) -> None:
        self.decompressed_size = decompressed_size
        self.max_size = max_size

    def __str__(self) -> str:
        return (
            f"The number of bytes decompressed so far "
            f"({self.decompressed_size} B) exceeded the specified maximum "
            f"({self.max_size} B)."
        )


class _DecompressionBudgetExceeded(_DecompressionMaxSizeExceeded):
    """Raised when the cumulative decompression work of a response exceeds
    its budget.

    ``layer_sizes`` holds the number of bytes produced by each decoded content
    coding, in decoding order. It never holds decompressed data.
    """

    def __init__(
        self, decompressed_size: int, max_size: int, layer_sizes: tuple[int, ...]
    ) -> None:
        super().__init__(decompressed_size, max_size)
        self.layer_sizes = layer_sizes

    def __str__(self) -> str:
        layers = ", ".join(f"{size} B" for size in self.layer_sizes)
        return (
            f"The cumulative decompression work ({self.decompressed_size} B) "
            f"exceeded the specified maximum ({self.max_size} B); produced "
            f"per content coding: {layers}."
        )


class _DecompressionBudget:
    """Cumulative decompression work of a single response.

    Every byte produced by a decoder is charged to the budget, regardless of
    the content coding that produced it or of how the compressed body is split
    into segments or members. The budget is shared by all the content codings
    of the response, and its ``max_size`` of ``0`` disables the limit.

    ``start_layer`` must be called before decoding each content coding, and the
    returned index must be passed to the matching decoder.
    """

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.decompressed_size = 0
        self._layer_sizes: list[int] = []

    @property
    def layer_sizes(self) -> tuple[int, ...]:
        return tuple(self._layer_sizes)

    def start_layer(self) -> int:
        self._layer_sizes.append(0)
        return len(self._layer_sizes) - 1

    def charge(self, size: int, layer: int) -> None:
        self.decompressed_size += size
        self._layer_sizes[layer] += size
        if self.max_size and self.decompressed_size > self.max_size:
            raise _DecompressionBudgetExceeded(
                self.decompressed_size, self.max_size, self.layer_sizes
            )


def _check_max_size(decompressed_size: int, max_size: int) -> None:
    if max_size and decompressed_size > max_size:
        raise _DecompressionMaxSizeExceeded(decompressed_size, max_size)


def _next_read_size(budget: _DecompressionBudget | None) -> int:
    """Return how many output bytes to ask a decoder for at most.

    When a budget is in use, asking for one byte more than what is left lets
    the next charge detect an overrun without producing past the boundary, so
    even small compressed inputs can reach the limit.
    """
    if budget is None or not budget.max_size:
        return _CHUNK_SIZE
    return min(_CHUNK_SIZE, budget.max_size - budget.decompressed_size + 1)


def _consume(
    budget: _DecompressionBudget | None,
    layer: int,
    size: int,
    decompressed_size: int,
    max_size: int,
) -> int:
    """Charge a produced chunk and return the local running total."""
    if budget is not None:
        budget.charge(size, layer)
        return decompressed_size + size
    decompressed_size += size
    _check_max_size(decompressed_size, max_size)
    return decompressed_size


def gunzip(
    data: bytes,
    *,
    max_size: int = 0,
    budget: _DecompressionBudget | None = None,
    layer: int = 0,
) -> bytes:
    """Gunzip the given data and return as much data as possible.

    This is resilient to CRC checksum errors.
    """
    f = GzipFile(fileobj=BytesIO(data))
    output_stream = BytesIO()
    try:
        chunk = b"."
        decompressed_size = 0
        while chunk:
            try:
                chunk = f.read1(_next_read_size(budget))
            except (OSError, EOFError, struct.error):
                # complete only if there is some data, otherwise re-raise
                # see issue 87 about catching struct.error
                # some pages are quite small so output_stream is empty
                if output_stream.getbuffer().nbytes > 0:
                    break
                raise
            decompressed_size = _consume(
                budget, layer, len(chunk), decompressed_size, max_size
            )
            output_stream.write(chunk)
        return output_stream.getvalue()
    finally:
        f.close()
        output_stream.close()


def gzip_magic_number(response: Response) -> bool:
    return response.body[:3] == b"\x1f\x8b\x08"


def _inflate(
    data: bytes,
    *,
    max_size: int = 0,
    budget: _DecompressionBudget | None = None,
    layer: int = 0,
) -> bytes:
    output_stream = BytesIO()
    try:
        decompressor = zlib.decompressobj()
        try:
            first_chunk = decompressor.decompress(
                data, max_length=_next_read_size(budget)
            )
        except zlib.error:
            # to work with raw deflate content that may be sent by microsoft servers.
            decompressor = zlib.decompressobj(wbits=-15)
            first_chunk = decompressor.decompress(
                data, max_length=_next_read_size(budget)
            )
        decompressed_size = _consume(budget, layer, len(first_chunk), 0, max_size)
        output_stream.write(first_chunk)
        # Anything left in unconsumed_tail once the stream has ended is not part
        # of it, and feeding it back would neither consume it nor produce
        # output.
        while decompressor.unconsumed_tail and not decompressor.eof:
            output_chunk = decompressor.decompress(
                decompressor.unconsumed_tail,
                max_length=_next_read_size(budget),
            )
            decompressed_size = _consume(
                budget, layer, len(output_chunk), decompressed_size, max_size
            )
            output_stream.write(output_chunk)
        return output_stream.getvalue()
    finally:
        output_stream.close()


def _unbrotli(
    data: bytes,
    *,
    max_size: int = 0,
    budget: _DecompressionBudget | None = None,
    layer: int = 0,
) -> bytes:
    output_stream = BytesIO()
    try:
        decompressor = brotli.Decompressor()
        # brotli emits whole internal blocks, so the requested limit is a lower
        # bound on the size of the produced chunk.
        first_chunk = decompressor.process(
            data, output_buffer_limit=_next_read_size(budget)
        )
        decompressed_size = _consume(budget, layer, len(first_chunk), 0, max_size)
        output_stream.write(first_chunk)
        while not decompressor.is_finished():
            output_chunk = decompressor.process(
                b"", output_buffer_limit=_next_read_size(budget)
            )
            if not output_chunk:
                break
            decompressed_size = _consume(
                budget, layer, len(output_chunk), decompressed_size, max_size
            )
            output_stream.write(output_chunk)
        return output_stream.getvalue()
    finally:
        output_stream.close()


def _unzstd(
    data: bytes,
    *,
    max_size: int = 0,
    budget: _DecompressionBudget | None = None,
    layer: int = 0,
) -> bytes:
    stream_reader = zstd.ZstdFile(BytesIO(data))
    output_stream = BytesIO()
    try:
        output_chunk = b"."
        decompressed_size = 0
        while output_chunk:
            try:
                output_chunk = stream_reader.read(_next_read_size(budget))
            except EOFError:
                # Return as much data as possible out of a truncated response.
                break
            decompressed_size = _consume(
                budget, layer, len(output_chunk), decompressed_size, max_size
            )
            output_stream.write(output_chunk)
        return output_stream.getvalue()
    finally:
        stream_reader.close()
        output_stream.close()
