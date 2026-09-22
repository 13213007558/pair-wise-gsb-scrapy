from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from warnings import warn

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils.conf import _job_dir
from scrapy.utils.deprecate import method_is_overridden
from scrapy.utils.request import (
    RequestFingerprinter,
    RequestFingerprinterProtocol,
    referer_str,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from twisted.internet.defer import Deferred

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler
    from scrapy.http.request import Request
    from scrapy.spiders import Spider


_SIZE_BYTES = 2
_DEPTH_SIZE_BYTES = 4
_DEPTH_FILE_MAGIC = b"Scrapy request depths v1\n"


def _read_fingerprints(data: bytes) -> Iterator[bytes]:
    pos = 0
    while pos + _SIZE_BYTES <= len(data):
        size = int.from_bytes(data[pos : pos + _SIZE_BYTES], "big")
        pos += _SIZE_BYTES
        fingerprint = data[pos : pos + size]
        if len(fingerprint) < size:
            # Truncated by an unclean shutdown.
            return
        yield fingerprint
        pos += size


def _read_fingerprint_depths(data: bytes) -> dict[bytes, int]:
    if not data:
        return {}
    if not data.startswith(_DEPTH_FILE_MAGIC):
        raise ValueError("Unsupported request depth file format")

    depths: dict[bytes, int] = {}
    pos = len(_DEPTH_FILE_MAGIC)
    while pos + _DEPTH_SIZE_BYTES + _SIZE_BYTES <= len(data):
        depth = int.from_bytes(
            data[pos : pos + _DEPTH_SIZE_BYTES], "big", signed=True
        )
        pos += _DEPTH_SIZE_BYTES
        size = int.from_bytes(data[pos : pos + _SIZE_BYTES], "big")
        pos += _SIZE_BYTES
        fingerprint = data[pos : pos + size]
        if len(fingerprint) < size:
            raise ValueError("Truncated request depth file")
        stored_depth = depths.get(fingerprint)
        if stored_depth is None or depth < stored_depth:
            depths[fingerprint] = depth
        pos += size
    if pos != len(data):
        raise ValueError("Truncated request depth file")
    return depths


class BaseDupeFilter:
    """Dummy duplicate request filtering class (:setting:`DUPEFILTER_CLASS`)
    that does not filter out any request."""

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls()

    def request_seen(self, request: Request) -> bool:
        return False

    def open(self) -> Deferred[None] | None:
        pass

    def close(self, reason: str) -> Deferred[None] | None:
        pass

    def log(self, request: Request, spider: Spider) -> None:
        """Log that a request has been filtered"""
        warn(
            "Calling BaseDupeFilter.log() is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )


class RFPDupeFilter(BaseDupeFilter):
    """Duplicate request filtering class (:setting:`DUPEFILTER_CLASS`) that
    filters out requests with the canonical
    (:func:`w3lib.url.canonicalize_url`) :attr:`~scrapy.http.Request.url`,
    :attr:`~scrapy.http.Request.method` and :attr:`~scrapy.http.Request.body`.

    Job directory contents
    ======================

    .. warning:: The files that this class generates in the :ref:`job directory
        <job-dir>` are an implementation detail, and may change without a
        warning in a future version of Scrapy. Do not rely on the following
        information for anything other than debugging purposes.

    When using :setting:`JOBDIR`, seen fingerprints are tracked in a binary
    file named :file:`requests.seen` in the :ref:`job directory <job-dir>`,
    where each fingerprint is stored as its big-endian, 2-byte length followed
    by the fingerprint itself.

    When :setting:`DUPEFILTER_SHORTEST_DEPTH` is enabled, fingerprints and
    their shortest depth are instead tracked in :file:`requests.depth.seen`.
    That file starts with a format marker, followed by records containing a
    big-endian, 4-byte signed depth, the big-endian, 2-byte fingerprint length
    and the fingerprint itself.
    """

    def __init__(
        self,
        path: str | None = None,
        debug: bool = False,
        *,
        fingerprinter: RequestFingerprinterProtocol | None = None,
        shortest_depth: bool = False,
    ) -> None:
        self.file = None
        self.fingerprinter: RequestFingerprinterProtocol = (
            fingerprinter or RequestFingerprinter()
        )
        self._shortest_depth = shortest_depth
        self._fingerprints: set[bytes] | dict[bytes, int] = (
            {} if shortest_depth else set()
        )
        self.logdupes = True
        self.debug = debug
        self.logger = logging.getLogger(__name__)
        self._legacy_fingerprint = method_is_overridden(
            type(self), RFPDupeFilter, "request_fingerprint"
        )
        if self._legacy_fingerprint:
            warn(
                "Overriding RFPDupeFilter.request_fingerprint() is deprecated,"
                " set the REQUEST_FINGERPRINTER_CLASS setting instead.",
                ScrapyDeprecationWarning,
                stacklevel=2,
            )
        if path:
            path_obj = Path(path)
            path_obj.mkdir(parents=True, exist_ok=True)
            legacy_path = path_obj / "requests.seen"
            depth_path = path_obj / "requests.depth.seen"
            if shortest_depth and legacy_path.exists():
                raise ValueError(
                    "Cannot use DUPEFILTER_SHORTEST_DEPTH with a job directory "
                    "created without it; use a new JOBDIR"
                )
            if not shortest_depth and depth_path.exists():
                raise ValueError(
                    "Cannot use a job directory created with "
                    "DUPEFILTER_SHORTEST_DEPTH without it; use a new JOBDIR"
                )
            file_path = depth_path if shortest_depth else legacy_path
            self.file = file_path.open("a+b")
            self.file.seek(0)
            data = self.file.read()
            try:
                if shortest_depth:
                    self._fingerprints = _read_fingerprint_depths(data)
                    if not data:
                        self.file.write(_DEPTH_FILE_MAGIC)
                else:
                    fingerprints = self._fingerprints
                    assert isinstance(fingerprints, set)
                    fingerprints.update(_read_fingerprints(data))
            except Exception:
                self.file.close()
                self.file = None
                raise

    @property
    def fingerprints(self) -> frozenset[str]:
        warn(
            "RFPDupeFilter.fingerprints is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return frozenset(fp.hex() for fp in self._fingerprints)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        debug = crawler.settings.getbool("DUPEFILTER_DEBUG")
        kwargs: dict[str, bool] = {}
        if crawler.settings.getbool("DUPEFILTER_SHORTEST_DEPTH"):
            kwargs["shortest_depth"] = True
        return cls(
            _job_dir(crawler.settings),
            debug,
            fingerprinter=crawler.request_fingerprinter,
            **kwargs,
        )

    def request_seen(self, request: Request) -> bool:
        fp = self._fingerprint(request)
        if self._shortest_depth:
            assert isinstance(self._fingerprints, dict)
            depth = request.meta.get("depth", 0)
            stored_depth = self._fingerprints.get(fp)
            if stored_depth is not None and depth >= stored_depth:
                return True
            self._fingerprints[fp] = depth
            if self.file:
                self.file.write(
                    depth.to_bytes(_DEPTH_SIZE_BYTES, "big", signed=True)
                    + len(fp).to_bytes(_SIZE_BYTES, "big")
                    + fp
                )
            return False
        assert isinstance(self._fingerprints, set)
        if fp in self._fingerprints:
            return True
        self._fingerprints.add(fp)
        if self.file:
            self.file.write(len(fp).to_bytes(_SIZE_BYTES, "big") + fp)
        return False

    def _fingerprint(self, request: Request) -> bytes:
        if self._legacy_fingerprint:
            return bytes.fromhex(self.request_fingerprint(request))
        return self.fingerprinter.fingerprint(request)

    def request_fingerprint(self, request: Request) -> str:
        """Returns a string that uniquely identifies the specified request."""
        return self.fingerprinter.fingerprint(request).hex()

    def close(self, reason: str) -> None:
        if self.file:
            self.file.close()

    def log(self, request: Request, spider: Spider) -> None:
        if self.debug:
            msg = "Filtered duplicate request: %(request)s (referer: %(referer)s)"
            args = {"request": request, "referer": referer_str(request)}
            self.logger.debug(msg, args, extra={"spider": spider})
        elif self.logdupes:
            msg = (
                "Filtered duplicate request: %(request)s"
                " - no more duplicates will be shown"
                " (see DUPEFILTER_DEBUG to show all duplicates)"
            )
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
            self.logdupes = False

        spider.crawler.stats.inc_value("dupefilter/filtered")
