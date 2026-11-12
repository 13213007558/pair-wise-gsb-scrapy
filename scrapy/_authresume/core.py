from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.utils.misc import build_from_crawler, load_object

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response

logger = logging.getLogger(__name__)

CREDENTIAL_REFERENCE_META_KEY = "credential_reference"

# Header names that carry a credential or a short-lived signature. They are
# stripped before a request reaches a persistent queue and re-added at
# download time, so a restart never replays a value captured on disk.
DEFAULT_SENSITIVE_HEADERS: tuple[bytes, ...] = (
    b"authorization",
    b"proxy-authorization",
    b"x-api-key",
    b"x-auth-token",
    b"x-amz-security-token",
    b"x-amz-signature",
)


class CredentialError(IgnoreRequest):
    """Base class for requests that cannot be re-authorized on resume.

    Subclasses :class:`~scrapy.exceptions.IgnoreRequest` so the request is not
    sent and reaches the spider errback (and the logs) with an explicit reason
    instead of being retried with a stale header or silently dropped.
    """


class CredentialRevokedError(CredentialError):
    """Raised when the referenced credential has been revoked or expired."""


class CredentialReferenceMissingError(CredentialError):
    """Raised for a legacy queued request that carries no credential
    reference, so it cannot be safely re-authorized."""


class CredentialProvider(ABC):
    """Resolves a non-sensitive credential reference into fresh auth material.

    The reference (e.g. an account or key id) is safe to persist; the secret
    itself is never stored in a request or written to the job directory.
    """

    @abstractmethod
    def get_credentials(
        self, reference: str
    ) -> Mapping[str | bytes, Any]:
        """Return fresh header values for *reference*.

        The mapping is applied to the request as headers. Raise
        :class:`CredentialRevokedError` (or :class:`~scrapy.exceptions.IgnoreRequest`)
        when the reference no longer maps to a usable credential.
        """
        raise NotImplementedError  # pragma: no cover


class AuthResumableRequest(Request):
    """A request that keeps only a credential reference when persisted.

    Sensitive headers live on the in-memory request while it is being built
    and downloaded, but :meth:`to_dict` removes them, so the serialized form
    stored in the :setting:`JOBDIR` queue contains the reference (in
    :attr:`meta`) and never the secret. A fresh value is resolved from the
    reference on resume by :class:`AuthRefreshMiddleware`.
    """

    def __init__(
        self,
        *args: Any,
        credential_reference: str | None = None,
        **kwargs: Any,
    ) -> None:
        meta = dict(kwargs.pop("meta", None) or {})
        if credential_reference is not None:
            meta[CREDENTIAL_REFERENCE_META_KEY] = credential_reference
        super().__init__(*args, meta=meta, **kwargs)

    def to_dict(self, *, spider: Spider | None = None) -> dict[str, Any]:
        d = super().to_dict(spider=spider)
        d["headers"] = {
            key: values
            for key, values in d["headers"].items()
            if _as_bytes(key).lower() not in DEFAULT_SENSITIVE_HEADERS
        }
        return d


def _as_bytes(name: str | bytes) -> bytes:
    return name.encode("utf-8") if isinstance(name, str) else bytes(name)


def _is_sensitive(header_name: Any, sensitive: frozenset[bytes]) -> bool:
    return _as_bytes(header_name).lower() in sensitive


def strip_sensitive_headers(
    request: Request, sensitive: frozenset[bytes] = frozenset(DEFAULT_SENSITIVE_HEADERS)
) -> dict[bytes, list[bytes]]:
    """Remove and return sensitive headers from *request* in place."""
    removed: dict[bytes, list[bytes]] = {}
    for name in list(request.headers.keys()):
        if _is_sensitive(name, sensitive):
            removed[bytes(name)] = list(request.headers.pop(name))
    return removed


class AuthRefreshMiddleware:
    """Re-authorizes resumed requests from their credential reference.

    Requests that hold a reference get a fresh set of sensitive headers from
    the credential provider, and any sensitive header already on the request
    is discarded so a value recovered from an old queue can never be sent.
    Requests without a reference (legacy on-disk entries) are rejected
    explicitly rather than downloaded unauthenticated.
    """

    def __init__(
        self,
        provider: CredentialProvider,
        sensitive_headers: frozenset[bytes] = frozenset(DEFAULT_SENSITIVE_HEADERS),
        quarantine_path: str | Path | None = None,
    ) -> None:
        self._provider = provider
        self._sensitive = sensitive_headers
        self._quarantine_path = (
            Path(quarantine_path) if quarantine_path is not None else None
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        provider_path = crawler.settings.get("AUTH_CREDENTIAL_PROVIDER")
        if not provider_path:
            from scrapy.exceptions import NotConfigured

            raise NotConfigured("AUTH_CREDENTIAL_PROVIDER is not set")
        provider = build_from_crawler(load_object(provider_path), crawler)
        quarantine = crawler.settings.get("AUTH_QUARANTINE_PATH")
        return cls(
            provider=provider,  # type: ignore[arg-type]
            quarantine_path=quarantine,
        )

    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Response | None:
        # Never trust a secret recovered from disk: drop it before resolving.
        strip_sensitive_headers(request, self._sensitive)

        reference = request.meta.get(CREDENTIAL_REFERENCE_META_KEY)
        if not reference:
            error = CredentialReferenceMissingError(
                f"Request {request.url} has no credential reference; refusing"
                " to send a legacy queued request without re-authorization"
            )
            self._quarantine(request, error)
            raise error

        try:
            headers = self._provider.get_credentials(reference)
        except IgnoreRequest as exc:
            self._quarantine(request, exc)
            raise
        except Exception as exc:
            error = CredentialRevokedError(
                f"Credential reference {reference!r} for {request.url} could"
                f" not be resolved: {exc}"
            )
            self._quarantine(request, error)
            raise error from exc

        for name, value in headers.items():
            if _is_sensitive(name, self._sensitive):
                request.headers[name] = value
        return None

    def _quarantine(self, request: Request, error: Exception) -> None:
        """Persist a non-sensitive description of a rejected request.

        The record keeps the URL and credential reference so an operator can
        account for the task, but contains no header or signature material.
        """
        if self._quarantine_path is None:
            return
        record = {
            "url": request.url,
            CREDENTIAL_REFERENCE_META_KEY: request.meta.get(
                CREDENTIAL_REFERENCE_META_KEY
            ),
            "error": type(error).__name__,
            "reason": str(error),
        }
        self._quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        with self._quarantine_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        logger.error(
            "Quarantined request %(url)s: %(reason)s",
            {"url": request.url, "reason": error},
            extra={"spider": None},
        )


def migrate_legacy_queue(
    entries: list[dict[str, Any]],
    *,
    default_reference: str | None = None,
) -> list[dict[str, Any]]:
    """Strip secrets from legacy serialized requests before they are re-queued.

    Older queues stored full request headers, including auth headers. This
    removes sensitive headers from each serialized dict and, when a
    *default_reference* is given, attaches the credential reference needed to
    re-authorize on resume. Entries that cannot be associated with a reference
    are left without one so :class:`AuthRefreshMiddleware` rejects them with an
    explicit, recoverable error instead of sending a stale header.
    """
    migrated: list[dict[str, Any]] = []
    for entry in entries:
        entry = dict(entry)
        headers = entry.get("headers") or {}
        entry["headers"] = {
            key: values
            for key, values in headers.items()
            if _as_bytes(key).lower() not in DEFAULT_SENSITIVE_HEADERS
        }
        if default_reference is not None:
            meta = dict(entry.get("meta") or {})
            meta.setdefault(CREDENTIAL_REFERENCE_META_KEY, default_reference)
            entry["meta"] = meta
        migrated.append(entry)
    return migrated
