from __future__ import annotations

import hashlib
import pickle
from pathlib import Path

from .fixtures import FakeDownloader, build_jobdir, write_policy
from .preflight import EXIT_CHECK_FAILED, GateResult, print_audit, startup_gate
from .queue_scan import QUEUE_DIRNAME

SCOPES_V1 = {
    "version": 1,
    "issued_at": "2026-11-01T00:00:00Z",
    "allow": [
        {"host": "shop.example", "path_prefix": "/products", "scope": "read"},
        {"host": "shop.example", "path_prefix": "/orders", "scope": "read"},
        {"host": "shop.example", "path_prefix": "/admin", "scope": "admin"},
    ],
    "deny": [],
}

SCOPES_TIGHTENED = {
    "version": 2,
    "issued_at": "2026-11-10T00:00:00Z",
    "allow": [
        {"host": "shop.example", "path_prefix": "/products", "scope": "read"},
    ],
    "deny": [
        {"host": "shop.example", "path_prefix": "/admin", "scope": "admin"},
        {"host": "shop.example", "path_prefix": "/products/9", "scope": "read"},
    ],
}

# (url, scope, stored authz_version, negated-priority)
REQUESTS = [
    ("https://shop.example/products/1001", "read", 2, 0),
    ("https://shop.example/products/1002", "read", 2, 0),
    ("https://shop.example/products/9999", "read", 2, 0),
    ("https://shop.example/orders/2002", "read", 1, 0),
    ("https://shop.example/admin/3003", "admin", 2, -10),
]


def _banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _snapshot(jobdir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    root = jobdir / QUEUE_DIRNAME
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(jobdir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _tamper_request(jobdir: Path) -> None:
    """Rewrite one queued request so its authorization meta is unusable."""
    queue_file = jobdir / QUEUE_DIRNAME / "0"
    data = queue_file.read_bytes()
    count = int.from_bytes(data[:4], "big")
    payloads: list[bytes] = []
    cursor = len(data)
    for _ in range(count):
        size = int.from_bytes(data[cursor - 4 : cursor], "big")
        start = cursor - 4 - size
        payloads.append(data[start : start + size])
        cursor = start
    payloads.reverse()

    broken = pickle.loads(payloads[-1])  # noqa: S301
    broken["meta"] = ["not", "an", "object"]
    payloads[-1] = pickle.dumps(broken, protocol=4)

    rebuilt = count.to_bytes(4, "big")
    for payload in payloads:
        rebuilt += payload + len(payload).to_bytes(4, "big")
    queue_file.write_bytes(rebuilt)


def _send_or_block(downloader: FakeDownloader, jobdir: Path, gate: GateResult) -> int:
    if not gate.allowed_to_start:
        try:
            downloader.drain(jobdir, gate.allowed_to_start)
        except RuntimeError:
            return 0
    return downloader.drain(jobdir, gate.allowed_to_start)


def main(tmp_root: Path) -> int:
    downloader = FakeDownloader()
    worst_exit = 0
    policy_v1 = write_policy(tmp_root / "policy-v1.json", SCOPES_V1)

    _banner("Scenario 1: first start (no persisted queue yet)")
    jobdir = tmp_root / "first"
    jobdir.mkdir(parents=True)
    gate = startup_gate(jobdir, policy_v1)
    print_audit(gate)
    sent = downloader.drain(jobdir, gate.allowed_to_start)
    print(f"requests_sent_after_gate={sent}")
    assert gate.allowed_to_start and sent == 0
    worst_exit = max(worst_exit, gate.exit_code)

    _banner("Scenario 2: normal resume (policy and requests still on v1)")
    v1_requests = [
        ("https://shop.example/products/1001", "read", 1, 0),
        ("https://shop.example/orders/2002", "read", 1, 0),
        ("https://shop.example/admin/3003", "admin", 1, -10),
    ]
    jobdir = build_jobdir(tmp_root, "resume", v1_requests)
    gate = startup_gate(jobdir, policy_v1)
    print_audit(gate)
    sent = downloader.drain(jobdir, gate.allowed_to_start)
    print(f"requests_sent_after_gate={sent}")
    assert gate.allowed_to_start and gate.report is not None
    counts = (
        gate.report.scanned,
        gate.report.retained,
        gate.report.quarantined,
    )
    assert counts == (3, 3, 0)
    assert sent == 3
    worst_exit = max(worst_exit, gate.exit_code)

    _banner("Scenario 3: resume after authorization tightened (v1 -> v2)")
    jobdir = build_jobdir(tmp_root, "tightened", REQUESTS)
    policy_v2 = write_policy(tmp_root / "policy-v2.json", SCOPES_TIGHTENED)
    gate = startup_gate(jobdir, policy_v2)
    print_audit(gate)
    sent = _send_or_block(downloader, jobdir, gate)
    print(f"requests_sent_after_gate={sent}")
    assert gate.allowed_to_start and gate.report is not None
    report = gate.report
    assert report.scanned == 5
    assert report.retained == 2
    assert report.quarantined == 3
    assert sent == report.retained == 2
    backup = jobdir / "requests.queue.preflight-backup"
    print(f"original_queue_preserved={'yes' if backup.exists() else 'no'}")
    assert backup.exists()
    quarantine = jobdir / "preflight-quarantine.jsonl"
    lines = quarantine.read_text(encoding="utf-8").strip().splitlines()
    print(f"reviewable_quarantine_records={len(lines)}")
    print("quarantine file (masked, one JSON record per line):")
    print(quarantine.read_text(encoding="utf-8"), end="")
    assert len(lines) == 3
    worst_exit = max(worst_exit, gate.exit_code)

    _banner("Scenario 4a: check failure (malformed local policy fails closed)")
    jobdir = build_jobdir(tmp_root, "bad-policy", v1_requests)
    before = _snapshot(jobdir)
    bad_policy = tmp_root / "policy-broken.json"
    bad_policy.write_text("{not valid json", encoding="utf-8")
    gate = startup_gate(jobdir, bad_policy)
    print_audit(gate)
    sent = _send_or_block(downloader, jobdir, gate)
    print(f"requests_sent_after_gate={sent}")
    assert not gate.allowed_to_start and gate.exit_code == EXIT_CHECK_FAILED
    assert sent == 0 and _snapshot(jobdir) == before
    worst_exit = max(worst_exit, gate.exit_code)

    _banner("Scenario 4b: check failure (corrupt queue, zero requests sent)")
    jobdir = build_jobdir(tmp_root, "broken", v1_requests)
    before = _snapshot(jobdir)
    _tamper_request(jobdir)
    tampered = _snapshot(jobdir)
    gate = startup_gate(jobdir, policy_v1)
    print_audit(gate)
    sent = _send_or_block(downloader, jobdir, gate)
    print(f"requests_sent_after_gate={sent}")
    assert not gate.allowed_to_start and gate.exit_code == EXIT_CHECK_FAILED
    assert sent == 0
    unchanged = "yes" if _snapshot(jobdir) == tampered else "no"
    print(f"queue_unchanged_since_failure={unchanged}")
    print(
        "original_queue_still_present="
        f"{'yes' if before.keys() <= _snapshot(jobdir).keys() else 'no'}"
    )
    staging = jobdir / "requests.queue.staging"
    print(f"staging_dir_left_behind={'yes' if staging.exists() else 'no'}")
    assert _snapshot(jobdir) == tampered
    assert not staging.exists()
    worst_exit = max(worst_exit, gate.exit_code)

    return worst_exit
