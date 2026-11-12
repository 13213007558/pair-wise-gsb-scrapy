from __future__ import annotations

import asyncio
import json
import pickle
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import Mock

import pytest
from twisted.python.failure import Failure

from scrapy import Request
from scrapy._authresume.core import (
    CREDENTIAL_REFERENCE_META_KEY,
    DEFAULT_SENSITIVE_HEADERS,
    AuthRefreshMiddleware,
    AuthResumableRequest,
    CredentialError,
    CredentialProvider,
    CredentialReferenceMissingError,
    CredentialRevokedError,
    migrate_legacy_queue,
    strip_sensitive_headers,
)
from scrapy.core.scheduler import Scheduler
from scrapy.crawler import Crawler
from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http.response import Response
from scrapy.spiders import Spider
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.request import request_from_dict
from scrapy.utils.test import get_crawler
from tests.utils.downloader import MockDownloader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

SECRET = "Bearer TOP-SECRET-VALUE"
SENSITIVE_SAMPLE = [
    ("Authorization", SECRET),
    ("X-Api-Key", "api-key-123"),
    ("X-Amz-Signature", "sig-456"),
]


class _MockCrawler(Crawler):
    def __init__(self, jobdir: Path) -> None:
        settings = {
            "SCHEDULER_DEBUG": False,
            "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleLifoDiskQueue",
            "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.LifoMemoryQueue",
            "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.ScrapyPriorityQueue",
            "JOBDIR": str(jobdir),
            "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
        }
        super().__init__(Spider, settings)
        self.engine = Mock(downloader=MockDownloader())
        self.stats = load_object(self.settings["STATS_CLASS"])(self)


@asynccontextmanager
async def _open_scheduler(jobdir: Path) -> AsyncGenerator[Scheduler]:
    crawler = _MockCrawler(jobdir)
    scheduler = build_from_crawler(Scheduler, crawler)
    spider = Spider.from_crawler(crawler, name="spider")
    await ensure_awaitable(scheduler.open(spider))
    try:
        yield scheduler
    finally:
        await ensure_awaitable(scheduler.close("finished"))
        await crawler.stop_async()
        crawler.engine.downloader.close()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class StaticCredentialProvider(CredentialProvider):
    """In-memory provider; a reference in *revoked* is treated as unusable."""

    def __init__(self, revoked: frozenset[str] = frozenset()) -> None:
        self._revoked = revoked
        self.requested: list[str] = []

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(frozenset(crawler.settings.get("AUTH_REVOKED_REFERENCES", ())))

    def get_credentials(
        self, reference: str
    ) -> Mapping[str | bytes, Any]:
        self.requested.append(reference)
        if reference in self._revoked:
            raise CredentialRevokedError(f"credential {reference!r} was revoked")
        return {"Authorization": f"Bearer fresh-{reference}"}


def _queue_payloads(jobdir: Path) -> list[dict[str, Any]]:
    """Decode the pickled request dicts stored by the LIFO disk queue."""
    payloads: list[dict[str, Any]] = []
    for path in (jobdir / "requests.queue").rglob("*"):
        if not path.is_file() or path.name == "active.json":
            continue
        data = path.read_bytes()
        end = len(data)
        while end and data[end - 1 : end] == b"\x00":
            end -= 1
        start = data.index(b"\x80")
        size = struct.unpack(">I", data[end - 4 : end])[0]
        payloads.append(pickle.loads(data[start : start + size]))
    return payloads


def _all_disk_bytes(jobdir: Path) -> bytes:
    blob = b""
    for path in (jobdir / "requests.queue").rglob("*"):
        if path.is_file():
            blob += path.read_bytes()
    return blob


class TestAuthResumableRequest:
    def test_reference_stored_in_meta(self) -> None:
        request = AuthResumableRequest(
            "https://example.com/a",
            credential_reference="acct-1",
            headers=SENSITIVE_SAMPLE,
        )
        assert request.meta[CREDENTIAL_REFERENCE_META_KEY] == "acct-1"
        # The in-memory request keeps the secret so a non-persisted crawl works.
        assert request.headers.get("Authorization") == SECRET.encode()

    def test_to_dict_strips_sensitive_headers(self) -> None:
        request = AuthResumableRequest(
            "https://example.com/a",
            credential_reference="acct-1",
            headers=[*SENSITIVE_SAMPLE, ("X-Trace", "keep-me")],
        )
        d = request.to_dict()
        persisted_names = {bytes(k).lower() for k in d["headers"]}
        assert persisted_names.isdisjoint(DEFAULT_SENSITIVE_HEADERS)
        assert d["headers"] == {b"X-Trace": [b"keep-me"]}
        assert d["meta"][CREDENTIAL_REFERENCE_META_KEY] == "acct-1"

    def test_roundtrip_rebuilds_without_secret(self) -> None:
        request = AuthResumableRequest(
            "https://example.com/a",
            credential_reference="acct-1",
            headers=SENSITIVE_SAMPLE,
        )
        restored = request_from_dict(request.to_dict())
        assert isinstance(restored, AuthResumableRequest)
        assert restored.meta[CREDENTIAL_REFERENCE_META_KEY] == "acct-1"
        assert b"authorization" not in restored.headers

    def test_constructible_without_reference_for_deserialization(self) -> None:
        request = AuthResumableRequest("https://example.com/a")
        assert CREDENTIAL_REFERENCE_META_KEY not in request.meta


class TestStripSensitiveHeaders:
    def test_removes_only_sensitive_names_case_insensitive(self) -> None:
        request = Request(
            "https://example.com/a",
            headers=[("authorization", SECRET), ("X-Keep", "v")],
        )
        removed = strip_sensitive_headers(request)
        assert b"authorization" in removed
        assert b"authorization" not in request.headers
        assert request.headers.get("X-Keep") == b"v"


class TestAuthRefreshMiddleware:
    def test_refresh_injects_fresh_header(self) -> None:
        provider = StaticCredentialProvider()
        middleware = AuthRefreshMiddleware(provider)
        request = request_from_dict(
            AuthResumableRequest(
                "https://example.com/a",
                credential_reference="acct-1",
                headers={"Authorization": SECRET},
            ).to_dict()
        )
        assert middleware.process_request(request) is None
        assert request.headers.get("Authorization") == b"Bearer fresh-acct-1"
        assert provider.requested == ["acct-1"]

    def test_disk_recovered_secret_is_never_sent(self) -> None:
        # A legacy value already on the request must be replaced, not replayed.
        request = AuthResumableRequest(
            "https://example.com/a",
            credential_reference="acct-1",
            headers={"Authorization": "Bearer stale-on-disk"},
        )
        AuthRefreshMiddleware(StaticCredentialProvider()).process_request(request)
        assert request.headers.get("Authorization") == b"Bearer fresh-acct-1"

    def test_revoked_credential_raises_explainable_error(self) -> None:
        provider = StaticCredentialProvider(revoked=frozenset({"acct-dead"}))
        middleware = AuthRefreshMiddleware(provider)
        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-dead"
        )
        with pytest.raises(CredentialRevokedError) as exc_info:
            middleware.process_request(request)
        assert "acct-dead" in str(exc_info.value)
        assert isinstance(exc_info.value, CredentialError)
        assert isinstance(exc_info.value, IgnoreRequest)
        # No header is attached to a request that fails re-authorization.
        assert b"authorization" not in request.headers

    def test_provider_failure_is_wrapped_as_revocation(self) -> None:
        class BrokenProvider(StaticCredentialProvider):
            def get_credentials(self, reference: str):
                raise RuntimeError("vault unreachable")

        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-1"
        )
        with pytest.raises(CredentialRevokedError, match="vault unreachable"):
            AuthRefreshMiddleware(BrokenProvider()).process_request(request)

    def test_legacy_request_without_reference_is_rejected(self) -> None:
        request = Request(
            "https://example.com/legacy", headers={"Authorization": SECRET}
        )
        with pytest.raises(CredentialReferenceMissingError):
            AuthRefreshMiddleware(StaticCredentialProvider()).process_request(request)
        # The stale secret is removed and the request is not sent.
        assert b"authorization" not in request.headers

    def test_quarantine_records_non_sensitive_record(self, tmp_path: Path) -> None:
        quarantine = tmp_path / "quarantine.jsonl"
        middleware = AuthRefreshMiddleware(
            StaticCredentialProvider(revoked=frozenset({"acct-dead"})),
            quarantine_path=quarantine,
        )
        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-dead"
        )
        with pytest.raises(CredentialRevokedError):
            middleware.process_request(request)
        records = [json.loads(line) for line in quarantine.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["url"] == "https://example.com/a"
        assert records[0][CREDENTIAL_REFERENCE_META_KEY] == "acct-dead"
        assert records[0]["error"] == "CredentialRevokedError"
        assert SECRET not in quarantine.read_text()

    def test_from_crawler_requires_provider_setting(self) -> None:
        crawler = get_crawler(Spider)
        with pytest.raises(NotConfigured):
            AuthRefreshMiddleware.from_crawler(crawler)

    def test_from_crawler_builds_configured_provider(self) -> None:
        crawler = get_crawler(
            Spider,
            {
                "AUTH_CREDENTIAL_PROVIDER": (
                    "tests.test_authresume.StaticCredentialProvider"
                ),
                "AUTH_REVOKED_REFERENCES": ["acct-dead"],
            },
        )
        middleware = build_from_crawler(AuthRefreshMiddleware, crawler)
        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-dead"
        )
        with pytest.raises(CredentialRevokedError):
            middleware.process_request(request)


class TestLegacyMigration:
    def test_strips_secret_and_attaches_reference(self) -> None:
        legacy = [
            Request(
                "https://example.com/old",
                headers={"Authorization": SECRET, "X-Keep": "v"},
            ).to_dict()
        ]
        migrated = migrate_legacy_queue(legacy, default_reference="acct-1")
        assert migrated[0]["headers"] == {b"X-Keep": [b"v"]}
        assert (
            migrated[0]["meta"][CREDENTIAL_REFERENCE_META_KEY] == "acct-1"
        )

    def test_entry_without_reference_stays_rejectable(self) -> None:
        legacy = [
            Request("https://example.com/old", headers={"Authorization": SECRET}).to_dict()
        ]
        migrated = migrate_legacy_queue(legacy)
        assert migrated[0]["headers"] == {}
        assert CREDENTIAL_REFERENCE_META_KEY not in (migrated[0].get("meta") or {})
        restored = request_from_dict(migrated[0])
        with pytest.raises(CredentialReferenceMissingError):
            AuthRefreshMiddleware(StaticCredentialProvider()).process_request(restored)

    def test_does_not_mutate_input(self) -> None:
        legacy = [
            Request("https://example.com/old", headers={"Authorization": SECRET}).to_dict()
        ]
        migrate_legacy_queue(legacy, default_reference="acct-1")
        assert b"Authorization" in legacy[0]["headers"]


class TestSchedulerPersistence:
    def test_close_and_resume_keeps_request_order(self, tmp_path: Path) -> None:
        urls = [f"https://example.com/{i}" for i in range(4)]

        async def enqueue() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                for index, url in enumerate(urls):
                    scheduler.enqueue_request(
                        AuthResumableRequest(
                            url,
                            credential_reference=f"acct-{index}",
                            headers={"Authorization": SECRET},
                        )
                    )

        async def dequeue() -> list[Request]:
            async with _open_scheduler(tmp_path) as scheduler:
                resumed: list[Request] = []
                while request := scheduler.next_request():
                    resumed.append(request)
                return resumed

        _run(enqueue())
        resumed = _run(dequeue())

        # Default internal queue is LIFO: last enqueued is first dequeued.
        assert [request.url for request in resumed] == urls[::-1]
        assert all(isinstance(request, AuthResumableRequest) for request in resumed)
        assert all(
            request.meta[CREDENTIAL_REFERENCE_META_KEY] == f"acct-{index}"
            for index, request in zip(range(3, -1, -1), resumed)
        )

    def test_no_secret_on_disk(self, tmp_path: Path) -> None:
        async def enqueue() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                scheduler.enqueue_request(
                    AuthResumableRequest(
                        "https://example.com/secret",
                        credential_reference="acct-1",
                        headers=SENSITIVE_SAMPLE,
                    )
                )

        _run(enqueue())

        blob = _all_disk_bytes(tmp_path)
        for secret in (SECRET, "api-key-123", "sig-456"):
            assert secret.encode() not in blob
        # The non-sensitive reference is what survives on disk.
        assert b"acct-1" in blob

        for payload in _queue_payloads(tmp_path):
            assert payload["headers"] == {}
            assert payload["meta"][CREDENTIAL_REFERENCE_META_KEY] == "acct-1"
            assert payload["_class"].endswith("AuthResumableRequest")

    def test_resumed_requests_reauthorized_before_download(
        self, tmp_path: Path
    ) -> None:
        async def enqueue() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                scheduler.enqueue_request(
                    AuthResumableRequest(
                        "https://example.com/a",
                        credential_reference="acct-1",
                        headers={"Authorization": SECRET},
                    )
                )

        _run(enqueue())

        provider = StaticCredentialProvider()
        middleware = AuthRefreshMiddleware(provider)

        async def resume() -> Request:
            async with _open_scheduler(tmp_path) as scheduler:
                request = scheduler.next_request()
                assert request is not None
                assert b"authorization" not in request.headers
                middleware.process_request(request)
                return request

        request = _run(resume())
        assert request.headers.get("Authorization") == b"Bearer fresh-acct-1"
        assert provider.requested == ["acct-1"]

    def test_revoked_resume_stops_with_explainable_reject(
        self, tmp_path: Path
    ) -> None:
        async def enqueue() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                scheduler.enqueue_request(
                    AuthResumableRequest(
                        "https://example.com/a",
                        credential_reference="acct-dead",
                        headers={"Authorization": SECRET},
                    )
                )

        _run(enqueue())

        # A fresh process only has the on-disk reference; the secret is gone.
        provider = StaticCredentialProvider(revoked=frozenset({"acct-dead"}))
        middleware = AuthRefreshMiddleware(provider)

        async def resume() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                request = scheduler.next_request()
                assert request is not None
                with pytest.raises(CredentialRevokedError):
                    middleware.process_request(request)
                assert b"authorization" not in request.headers

        _run(resume())

    def test_plain_request_persists_but_is_rejected_on_resume(
        self, tmp_path: Path
    ) -> None:
        # Documents the old-queue boundary: a legacy entry without a reference
        # is not silently sent and not silently lost.
        async def enqueue() -> None:
            async with _open_scheduler(tmp_path) as scheduler:
                scheduler.enqueue_request(
                    Request(
                        "https://example.com/legacy",
                        headers={"Authorization": "Bearer legacy-secret"},
                    )
                )

        _run(enqueue())

        middleware = AuthRefreshMiddleware(StaticCredentialProvider())

        async def resume() -> Request:
            async with _open_scheduler(tmp_path) as scheduler:
                request = scheduler.next_request()
                assert request is not None
                with pytest.raises(CredentialReferenceMissingError):
                    middleware.process_request(request)
                assert b"authorization" not in request.headers
                return request

        _run(resume())


class TestDownloaderMiddlewareIntegration:
    """Drives the real downloader middleware manager so the rejection follows
    the same path the engine uses to feed a request errback."""

    @staticmethod
    async def _download(
        middleware: AuthRefreshMiddleware, request: Request
    ) -> tuple[Any, list[Request]]:
        downloaded: list[Request] = []

        async def download_func(_request: Request) -> Response:
            downloaded.append(_request)
            return Response(_request.url)

        crawler = get_crawler(Spider)
        manager = DownloaderMiddlewareManager(middleware, crawler=crawler)
        result = await manager.download_async(download_func, request)
        return result, downloaded

    def test_authorized_request_reaches_download(self) -> None:
        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-1"
        )
        response, downloaded = _run(
            self._download(AuthRefreshMiddleware(StaticCredentialProvider()), request)
        )
        assert isinstance(response, Response)
        assert downloaded == [request]
        assert request.headers.get("Authorization") == b"Bearer fresh-acct-1"

    def test_revoked_request_surfaces_as_failure_not_silent_drop(self) -> None:
        request = AuthResumableRequest(
            "https://example.com/a", credential_reference="acct-dead"
        )
        middleware = AuthRefreshMiddleware(
            StaticCredentialProvider(revoked=frozenset({"acct-dead"}))
        )

        async def collect_outcome() -> tuple[Failure, list[Request]]:
            try:
                _result, downloaded = await self._download(middleware, request)
            except CredentialRevokedError as exc:
                return Failure(exc), []
            raise AssertionError("revoked request was downloaded")

        failure, _ = _run(collect_outcome())
        assert isinstance(failure, Failure)
        assert failure.check(CredentialRevokedError)
        assert issubclass(CredentialRevokedError, IgnoreRequest)
        # The download function is never reached for a rejected request.
        assert request.headers.get("Authorization") is None
