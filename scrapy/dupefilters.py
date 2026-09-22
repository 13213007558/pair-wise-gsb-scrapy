from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
_POLICY_FILE_NAME = "requests.seen.policy.json"


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


def _policy_differences(stored: Any, current: Any, path: str = "policy") -> list[str]:
    if isinstance(stored, dict) and isinstance(current, dict):
        differences = []
        for key in sorted(stored.keys() | current.keys()):
            if key not in stored:
                differences.append(f"{path}.{key}: <missing> != {current[key]!r}")
            elif key not in current:
                differences.append(f"{path}.{key}: {stored[key]!r} != <missing>")
            else:
                differences.extend(
                    _policy_differences(stored[key], current[key], f"{path}.{key}")
                )
        return differences
    if stored != current:
        return [f"{path}: {stored!r} != {current!r}"]
    return []


def _check_fingerprint_policy(path: str, policy: dict[str, Any] | None) -> None:
    policy_path = Path(path, _POLICY_FILE_NAME)
    if policy is None:
        if policy_path.exists():
            raise ValueError(
                f"Cannot resume job in {path!r}: the current request "
                "fingerprinter does not provide a fingerprint policy, but "
                f"{policy_path} contains one"
            )
        return
    if policy_path.exists():
        try:
            stored_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Cannot resume job in {path!r}: cannot read the request "
                f"fingerprint policy from {policy_path}"
            ) from exc
        if stored_policy != policy:
            differences = "; ".join(_policy_differences(stored_policy, policy))
            raise ValueError(
                f"Cannot resume job in {path!r}: the request fingerprint "
                f"policy changed ({differences})"
            )
        return
    seen_path = Path(path, "requests.seen")
    if seen_path.exists():
        legacy_policy = RequestFingerprinter()._fingerprint_policy
        if (
            policy["fingerprinter"] == legacy_policy["fingerprinter"]
            and policy != legacy_policy
        ):
            differences = "; ".join(_policy_differences(legacy_policy, policy))
            raise ValueError(
                f"Cannot resume job in {path!r}: the request fingerprint "
                "policy file is missing, the stored fingerprints are from "
                f"the legacy default policy, and the current policy differs "
                f"({differences})"
            )
    policy_path.write_text(
        json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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

    The request fingerprinting policy is tracked in a JSON file named
    :file:`requests.seen.policy.json`. It contains the policy version and a
    summary of the settings that affect request fingerprints. Resuming a job
    with a different policy raises :exc:`ValueError` instead of reusing or
    discarding the stored fingerprints.
    """

    def __init__(
        self,
        path: str | None = None,
        debug: bool = False,
        *,
        fingerprinter: RequestFingerprinterProtocol | None = None,
    ) -> None:
        self.file = None
        self.fingerprinter: RequestFingerprinterProtocol = (
            fingerprinter or RequestFingerprinter()
        )
        self._fingerprints: set[bytes] = set()
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
            policy = getattr(self.fingerprinter, "_fingerprint_policy", None)
            _check_fingerprint_policy(path, policy)
            self.file = Path(path, "requests.seen").open("a+b")
            self.file.seek(0)
            self._fingerprints.update(_read_fingerprints(self.file.read()))

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
        return cls(
            _job_dir(crawler.settings),
            debug,
            fingerprinter=crawler.request_fingerprinter,
        )

    def request_seen(self, request: Request) -> bool:
        fp = self._fingerprint(request)
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
