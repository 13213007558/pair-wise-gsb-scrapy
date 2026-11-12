from __future__ import annotations

from gzip import BadGzipFile, GzipFile
from io import BytesIO
from pathlib import Path

import pytest
from w3lib.encoding import html_to_unicode

from scrapy.http import Response
from scrapy.utils._compression import (
    _DecompressionBudget,
    _DecompressionBudgetExceeded,
    gunzip,
    gzip_magic_number,
)
from tests import tests_datadir

SAMPLEDIR = Path(tests_datadir, "compressed")


def test_gunzip_basic():
    r1 = Response(
        "http://www.example.com",
        body=(SAMPLEDIR / "feed-sample1.xml.gz").read_bytes(),
    )
    assert gzip_magic_number(r1)

    r2 = Response("http://www.example.com", body=gunzip(r1.body))
    assert not gzip_magic_number(r2)
    assert len(r2.body) == 9950


def test_gunzip_truncated():
    text = gunzip((SAMPLEDIR / "truncated-crc-error.gz").read_bytes())
    assert text.endswith(b"</html")


def test_gunzip_no_gzip_file_raises():
    with pytest.raises(BadGzipFile):
        gunzip((SAMPLEDIR / "feed-sample1.xml").read_bytes())


def test_gunzip_truncated_short():
    r1 = Response(
        "http://www.example.com",
        body=(SAMPLEDIR / "truncated-crc-error-short.gz").read_bytes(),
    )
    assert gzip_magic_number(r1)

    r2 = Response("http://www.example.com", body=gunzip(r1.body))
    assert r2.body.endswith(b"</html>")
    assert not gzip_magic_number(r2)


def test_is_gzipped_empty():
    r1 = Response("http://www.example.com")
    assert not gzip_magic_number(r1)


def test_gunzip_illegal_eof():
    text = html_to_unicode(
        "charset=cp1252", gunzip((SAMPLEDIR / "unexpected-eof.gz").read_bytes())
    )[1]
    expected_text = (SAMPLEDIR / "unexpected-eof-output.txt").read_text(
        encoding="utf-8"
    )
    assert len(text) == len(expected_text)
    assert text == expected_text


def _gzip_bytes(data: bytes) -> bytes:
    buf = BytesIO()
    with GzipFile(fileobj=buf, mode="wb") as f:
        f.write(data)
    return buf.getvalue()


def test_budget_shared_across_calls_and_members():
    plain = b"a" * 5000
    body = _gzip_bytes(plain) + _gzip_bytes(plain)
    budget = _DecompressionBudget(9000)
    layer = budget.start_layer()
    with pytest.raises(_DecompressionBudgetExceeded) as exc_info:
        gunzip(body, budget=budget, layer=layer)
    assert exc_info.value.decompressed_size == 9001
    assert exc_info.value.layer_sizes == (9001,)
    assert budget.decompressed_size == 9001


def test_budget_allows_exact_limit():
    plain = b"b" * 5000
    body = _gzip_bytes(plain) + _gzip_bytes(plain)
    budget = _DecompressionBudget(10_000)
    layer = budget.start_layer()
    assert gunzip(body, budget=budget, layer=layer) == plain * 2
    assert budget.decompressed_size == 10_000
