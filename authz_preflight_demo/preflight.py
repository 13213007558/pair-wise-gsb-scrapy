from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .policy import PolicyError, load_policy
from .queue_scan import (
    MANIFEST_FILENAME,
    PreflightReport,
    QueueCheckError,
    queue_dir,
    run_preflight,
)

EXIT_OK = 0
EXIT_CHECK_FAILED = 2


@dataclass(frozen=True)
class GateResult:
    allowed_to_start: bool
    exit_code: int
    reason: str
    report: PreflightReport | None


def startup_gate(jobdir: str | Path, policy_path: str | Path) -> GateResult:
    """Authorize a job before any persisted request is handed to a downloader.

    The policy is loaded before the queue is touched: an unreadable or
    malformed policy fails closed, so no request can be sent on an unverified
    authorization.
    """
    try:
        policy = load_policy(policy_path)
    except PolicyError as exc:
        return GateResult(False, EXIT_CHECK_FAILED, f"policy_invalid:{exc}", None)

    try:
        report = run_preflight(jobdir, policy)
    except QueueCheckError as exc:
        return GateResult(False, EXIT_CHECK_FAILED, f"queue_check_failed:{exc}", None)
    return GateResult(True, EXIT_OK, "authorized", report)


def print_audit(gate: GateResult, stream: TextIO = sys.stdout) -> None:
    """Emit only counts and non-reversible identifiers, never raw URLs."""
    if not gate.allowed_to_start:
        print(f"decision=blocked reason={gate.reason}", file=stream)
        print("scanned=0 retained=0 quarantined=0", file=stream)
        return

    assert gate.report is not None
    report = gate.report
    print(f"decision={report.decision} policy_version={report.policy_version}")
    print(
        f"scanned={report.scanned} retained={report.retained} "
        f"quarantined={report.quarantined}"
    )
    for record in report.records:
        print(
            "quarantine "
            f"id={record.record_id} fp={record.request_fingerprint} "
            f"host={record.host} url={record.masked_url} scope={record.scope} "
            f"stored_version={record.stored_version} "
            f"policy_version={record.policy_version} reason={record.reason}"
        )


def manifest_path(jobdir: str | Path) -> Path:
    return Path(jobdir) / MANIFEST_FILENAME


def persisted_queue_exists(jobdir: str | Path) -> bool:
    return queue_dir(jobdir).exists()
