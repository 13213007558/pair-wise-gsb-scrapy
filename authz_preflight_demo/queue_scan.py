from __future__ import annotations

import json
import pickle
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from queuelib.queue import LifoDiskQueue

from .policy import Policy, fingerprint, mask_host

QUEUE_DIRNAME = "requests.queue"
STATE_FILENAME = "active.json"
BACKUP_DIRNAME = "requests.queue.preflight-backup"
QUARANTINE_FILENAME = "preflight-quarantine.jsonl"
MANIFEST_FILENAME = "preflight-manifest.json"
VERSION_META_KEY = "authz_version"
SCOPE_META_KEY = "authz_scope"


class QueueCheckError(Exception):
    """Raised when a persisted request cannot be safely inspected."""


@dataclass(frozen=True)
class QuarantinedRecord:
    record_id: str
    request_fingerprint: str
    host: str
    masked_url: str
    scope: str
    stored_version: int | None
    policy_version: int
    reason: str
    priority: int


@dataclass
class PreflightReport:
    decision: str
    policy_version: int
    scanned: int = 0
    retained: int = 0
    quarantined: int = 0
    quarantine_path: str | None = None
    records: list[QuarantinedRecord] = field(default_factory=list)


def queue_dir(jobdir: str | Path) -> Path:
    return Path(jobdir) / QUEUE_DIRNAME


def _stored_priorities(path: Path) -> list[int]:
    state_path = path / STATE_FILENAME
    if not state_path.exists():
        return [0]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueCheckError(f"{STATE_FILENAME} unreadable: {exc}") from exc
    if not isinstance(state, list) or not all(isinstance(p, int) for p in state):
        raise QueueCheckError(f"{STATE_FILENAME} must contain a list of priorities")
    return state


def _queue_files(queue_path: Path, priorities: list[int]) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for priority in priorities:
        candidate = queue_path / str(priority)
        if candidate.exists():
            found.append((priority, candidate))
    # A queue file left behind without a matching active.json entry must not
    # escape review, so pick up any other regular file in the queue directory.
    known = {path for _, path in found}
    for entry in sorted(queue_path.iterdir()):
        if entry.is_file() and entry.name != STATE_FILENAME and entry not in known:
            suffix = entry.name.removesuffix("s")
            try:
                priority = int(suffix)
            except ValueError:
                raise QueueCheckError(
                    f"unrecognized queue file: {entry.name}"
                ) from None
            found.append((priority, entry))
    return found


def _load_request(raw: bytes) -> dict[str, Any]:
    try:
        request = pickle.loads(raw)  # noqa: S301
    except (pickle.UnpicklingError, EOFError, AttributeError, ValueError) as exc:
        raise QueueCheckError(f"request payload not unpicklable: {exc}") from exc
    if not isinstance(request, dict) or "url" not in request:
        raise QueueCheckError("request payload is not a serialized request")
    return request


def _meta(request: dict[str, Any]) -> dict[str, Any]:
    meta = request.get("meta", {})
    if not isinstance(meta, dict):
        raise QueueCheckError("request meta is not an object")
    return meta


def _stored_version(meta: dict[str, Any]) -> int | None:
    value = meta.get(VERSION_META_KEY)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise QueueCheckError(f"{VERSION_META_KEY} meta must be an integer")
    return value


def _scope(meta: dict[str, Any]) -> str:
    value = meta.get(SCOPE_META_KEY)
    if not isinstance(value, str) or not value:
        raise QueueCheckError(f"{SCOPE_META_KEY} meta must be a non-empty string")
    return value


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


_SIZE_FORMAT = ">L"
_SIZE_SIZE = struct.calcsize(_SIZE_FORMAT)


def _read_payloads(path: Path) -> list[bytes]:
    """Parse a queuelib LIFO queue without opening it for writing.

    The on-disk format is a 4-byte count followed by payloads each terminated
    by their 4-byte length. Reading it directly leaves the original queue file
    untouched, which is what makes a mid-check failure non-destructive.
    """
    data = path.read_bytes()
    if len(data) < _SIZE_SIZE:
        raise QueueCheckError(f"queue file {path.name} is truncated")
    (count,) = struct.unpack(_SIZE_FORMAT, data[:_SIZE_SIZE])
    payloads: list[bytes] = []
    cursor = len(data)
    for _ in range(count):
        if cursor - _SIZE_SIZE < _SIZE_SIZE:
            raise QueueCheckError(f"queue file {path.name} is truncated")
        (size,) = struct.unpack(_SIZE_FORMAT, data[cursor - _SIZE_SIZE : cursor])
        start = cursor - _SIZE_SIZE - size
        if start < _SIZE_SIZE:
            raise QueueCheckError(f"queue file {path.name} is truncated")
        payloads.append(data[start : start + size])
        cursor = start
    if cursor != _SIZE_SIZE:
        raise QueueCheckError(f"queue file {path.name} count does not match data")
    return payloads


def run_preflight(jobdir: str | Path, policy: Policy) -> PreflightReport:
    queue_path = queue_dir(jobdir)
    if not queue_path.exists():
        # A first start has no persisted requests, so there is nothing to
        # authorize and nothing must be sent before this check returns.
        return PreflightReport(decision="empty", policy_version=policy.version)

    priorities = _stored_priorities(queue_path)
    files = _queue_files(queue_path, priorities)

    staging_path = queue_path.with_name(f"{QUEUE_DIRNAME}.staging")
    if staging_path.exists():
        # Left over from an interrupted earlier run; start the rebuild over
        # rather than appending unknown contents to it.
        _remove_tree(staging_path)
    staging_path.mkdir(parents=True)

    report = PreflightReport(decision="proceed", policy_version=policy.version)
    quarantine_path = Path(jobdir) / QUARANTINE_FILENAME
    seen: set[str] = set()
    open_targets: list[LifoDiskQueue] = []
    try:
        for priority, source_file in files:
            payloads = _read_payloads(source_file)
            if payloads:
                target = LifoDiskQueue(str(staging_path / str(priority)))
                open_targets.append(target)
                for payload in payloads:
                    request = _load_request(payload)
                    meta = _meta(request)
                    version = _stored_version(meta)
                    scope = _scope(meta)
                    report.scanned += 1

                    reason = _revocation_reason(version, policy, request["url"], scope)
                    if reason is None:
                        target.push(payload)
                        report.retained += 1
                    else:
                        record = _record(
                            request, scope, version, policy.version, reason, priority
                        )
                        if record.record_id in seen:
                            raise QueueCheckError("duplicate request id in queue")
                        seen.add(record.record_id)
                        report.records.append(record)
                report.quarantined = len(report.records)
        for target in open_targets:
            target.close()

        if report.records:
            report.quarantine_path = str(quarantine_path)
        _write_quarantine(quarantine_path, report.records)
        active_priorities = sorted(
            int(child.name.removesuffix("s"))
            for child in staging_path.iterdir()
            if child.is_file()
        )
        (staging_path / STATE_FILENAME).write_text(
            json.dumps(active_priorities), encoding="utf-8"
        )
        _write_manifest(jobdir, report)
    except BaseException:
        # Nothing has been moved yet: the original queue stays byte-for-byte
        # intact and no request has left the process. Close every staging
        # handle before deleting the directory so no file descriptor leaks.
        for target in open_targets:
            target.close()
        _remove_tree(staging_path)
        raise

    backup_path = Path(jobdir) / BACKUP_DIRNAME
    if backup_path.exists():
        _remove_tree(backup_path)
    queue_path.rename(backup_path)
    staging_path.rename(queue_path)
    return report


def _revocation_reason(
    stored_version: int | None,
    policy: Policy,
    url: str,
    scope: str,
) -> str | None:
    if stored_version is None:
        return "missing_authz_version"
    if stored_version != policy.version:
        return "authz_version_mismatch"
    if policy.is_revoked(url, scope):
        return "scope_revoked"
    return None


def _record(
    request: dict[str, Any],
    scope: str,
    stored_version: int | None,
    policy_version: int,
    reason: str,
    priority: int,
) -> QuarantinedRecord:
    url = request["url"]
    request_id = str(request.get("id") or fingerprint(url))
    host = _host_of(url)
    return QuarantinedRecord(
        record_id=f"qr_{fingerprint(request_id)}",
        request_fingerprint=fingerprint(request_id),
        host=mask_host(host),
        masked_url=f"{urlsplit(url).scheme}://{mask_host(host)}/{fingerprint(url)}",
        scope=scope,
        stored_version=stored_version,
        policy_version=policy_version,
        reason=reason,
        priority=priority,
    )


def _write_quarantine(path: Path, records: list[QuarantinedRecord]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.__dict__, sort_keys=True) + "\n")


def _write_manifest(jobdir: str | Path, report: PreflightReport) -> None:
    manifest = {
        "decision": report.decision,
        "policy_version": report.policy_version,
        "scanned": report.scanned,
        "retained": report.retained,
        "quarantined": report.quarantined,
    }
    (Path(jobdir) / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()
