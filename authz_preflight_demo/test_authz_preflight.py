from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import pytest

from authz_preflight_demo.fixtures import FakeDownloader, build_jobdir, write_policy
from authz_preflight_demo.policy import PolicyError, load_policy, mask_host
from authz_preflight_demo.preflight import (
    EXIT_CHECK_FAILED,
    print_audit,
    startup_gate,
)
from authz_preflight_demo.queue_scan import (
    QUEUE_DIRNAME,
    QueueCheckError,
    run_preflight,
)

POLICY_V2 = {
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

ENTRIES = [
    ("https://shop.example/products/1", "read", 2, 0),
    ("https://shop.example/products/2", "read", 2, 0),
    ("https://shop.example/products/9", "read", 2, 0),
    ("https://shop.example/orders/1", "read", 1, 0),
    ("https://shop.example/admin/1", "admin", 2, -10),
]


def _policy(tmp_path: Path) -> Path:
    return write_policy(tmp_path / "policy.json", POLICY_V2)


def _hash_tree(jobdir: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted((jobdir / QUEUE_DIRNAME).rglob("*")):
        if path.is_file():
            key = str(path.relative_to(jobdir / QUEUE_DIRNAME))
            hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_first_start_without_queue_is_allowed(tmp_path: Path) -> None:
    jobdir = tmp_path / "job"
    jobdir.mkdir()
    gate = startup_gate(jobdir, _policy(tmp_path))
    assert gate.allowed_to_start
    assert gate.exit_code == 0
    assert gate.report is not None
    assert gate.report.decision == "empty"
    assert gate.report.scanned == 0


def test_normal_resume_retains_every_request(tmp_path: Path) -> None:
    entries = [
        ("https://shop.example/products/1", "read", 2, 0),
        ("https://shop.example/products/2", "read", 2, 0),
    ]
    jobdir = build_jobdir(tmp_path, "job", entries)
    gate = startup_gate(jobdir, _policy(tmp_path))
    assert gate.allowed_to_start
    assert gate.report is not None
    assert (gate.report.scanned, gate.report.retained) == (2, 2)
    assert gate.report.quarantined == 0
    assert FakeDownloader().drain(jobdir, True) == 2


def test_tightened_policy_quarantines_each_revocation_reason(tmp_path: Path) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)
    gate = startup_gate(jobdir, _policy(tmp_path))
    assert gate.allowed_to_start
    report = gate.report
    assert report is not None
    assert report.scanned == 5
    assert report.retained == 2
    assert report.quarantined == 3
    reasons = sorted(record.reason for record in report.records)
    assert reasons == [
        "authz_version_mismatch",
        "scope_revoked",
        "scope_revoked",
    ]

    quarantine = jobdir / "preflight-quarantine.jsonl"
    lines = quarantine.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        record = json.loads(line)
        assert "shop.example" not in json.dumps(record)
        assert record["host"] == mask_host("shop.example")
        assert record["host"] != "shop.example"
        assert record["record_id"].startswith("qr_sha256:")


def test_retained_queue_only_holds_allowed_requests(tmp_path: Path) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)
    startup_gate(jobdir, _policy(tmp_path))
    assert FakeDownloader().drain(jobdir, True) == 2
    backup = jobdir / "requests.queue.preflight-backup"
    assert backup.exists()
    manifest = json.loads((jobdir / "preflight-manifest.json").read_text())
    assert manifest == {
        "decision": "proceed",
        "policy_version": 2,
        "quarantined": 3,
        "retained": 2,
        "scanned": 5,
    }


def test_audit_output_has_no_raw_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)
    gate = startup_gate(jobdir, _policy(tmp_path))
    print_audit(gate)
    out = capsys.readouterr().out
    assert "/products/1" not in out
    assert "shop.example" not in out
    assert "scanned=5 retained=2 quarantined=3" in out


def test_malformed_policy_fails_closed_and_keeps_queue(tmp_path: Path) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)
    before = _hash_tree(jobdir)
    bad_policy = tmp_path / "bad.json"
    bad_policy.write_text("{broken", encoding="utf-8")

    gate = startup_gate(jobdir, bad_policy)
    assert not gate.allowed_to_start
    assert gate.exit_code == EXIT_CHECK_FAILED
    assert gate.report is None
    assert _hash_tree(jobdir) == before
    with pytest.raises(RuntimeError):
        FakeDownloader().drain(jobdir, gate.allowed_to_start)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": "2", "issued_at": "x", "allow": [], "deny": []}, "integer"),
        ({"version": 2, "issued_at": "", "allow": [], "deny": []}, "issued_at"),
        (
            {"version": 2, "issued_at": "x", "allow": [{"host": "h"}], "deny": []},
            "path_prefix",
        ),
    ],
)
def test_policy_validation_errors(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    path = write_policy(tmp_path / "p.json", payload)
    with pytest.raises(PolicyError, match=message):
        load_policy(path)


def test_corrupt_queue_fails_closed_without_side_effects(tmp_path: Path) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)
    original_names = {
        str(path.relative_to(jobdir / QUEUE_DIRNAME))
        for path in (jobdir / QUEUE_DIRNAME).rglob("*")
        if path.is_file()
    }
    queue_file = jobdir / QUEUE_DIRNAME / "0"
    queue_file.write_bytes(queue_file.read_bytes()[:-7])
    tampered = _hash_tree(jobdir)

    with pytest.raises(QueueCheckError):
        run_preflight(jobdir, load_policy(_policy(tmp_path)))

    # The check must stop on the corrupt bytes rather than rewrite or delete
    # them: the on-disk queue after failure is exactly the tampered state.
    assert _hash_tree(jobdir) == tampered
    remaining_names = {
        str(path.relative_to(jobdir / QUEUE_DIRNAME))
        for path in (jobdir / QUEUE_DIRNAME).rglob("*")
        if path.is_file()
    }
    assert remaining_names == original_names
    assert not (jobdir / "requests.queue.staging").exists()
    assert not (jobdir / "preflight-quarantine.jsonl").exists()


def test_malformed_request_meta_blocks_start(tmp_path: Path) -> None:
    jobdir = build_jobdir(tmp_path, "job", ENTRIES)

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
    tampered = _hash_tree(jobdir)

    gate = startup_gate(jobdir, _policy(tmp_path))
    assert not gate.allowed_to_start
    assert gate.exit_code == EXIT_CHECK_FAILED
    # A closed gate means zero requests, and the bad queue is left in place
    # for an operator rather than being emptied by the failed check.
    with pytest.raises(RuntimeError):
        FakeDownloader().drain(jobdir, gate.allowed_to_start)
    assert _hash_tree(jobdir) == tampered
    assert not (jobdir / "requests.queue.staging").exists()
