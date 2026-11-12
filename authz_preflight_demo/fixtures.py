from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from scrapy import Request, Spider
from scrapy.squeues import PickleLifoDiskQueue

from .queue_scan import QUEUE_DIRNAME, STATE_FILENAME


class DemoSpider(Spider):
    name = "authz_demo_spider"

    def parse(self, response: Any, **kwargs: Any) -> Iterator[None]:
        yield None


class _CrawlerStub:
    spider = DemoSpider()
    settings: dict[str, Any] = {}


def write_policy(path: str | Path, payload: dict[str, Any]) -> Path:
    policy_path = Path(path)
    policy_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return policy_path


def build_jobdir(
    root: str | Path,
    name: str,
    entries: list[tuple[str, str, int, int]],
) -> Path:
    """Create a JOBDIR with the same on-disk layout as Scrapy's scheduler.

    Each entry is (url, scope, authz_version, negated-priority). One disk
    queue file is created per priority, plus an ``active.json`` state file
    listing them, exactly as ``ScrapyPriorityQueue`` does.
    """
    jobdir = Path(root) / name
    queue_path = jobdir / QUEUE_DIRNAME
    queue_path.mkdir(parents=True)
    per_priority: dict[int, list[tuple[str, str, int]]] = {}
    for url, scope, version, negated_priority in entries:
        per_priority.setdefault(negated_priority, []).append((url, scope, version))

    for negated_priority, requests in sorted(per_priority.items()):
        target = str(queue_path / str(negated_priority))
        queue = PickleLifoDiskQueue(_CrawlerStub(), target)
        for url, scope, version in requests:
            queue.push(
                Request(
                    url,
                    meta={"authz_scope": scope, "authz_version": version},
                )
            )
        queue.close()
    state = sorted(per_priority)
    (queue_path / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    return jobdir


class FakeDownloader:
    """Stands in for the network: it only counts what a real run would send."""

    def __init__(self) -> None:
        self.sent = 0

    def drain(self, jobdir: str | Path, gate_allowed: bool) -> int:
        if not gate_allowed:
            # The engine must never reach this point while the gate is closed.
            raise RuntimeError("downloader called while preflight gate is closed")
        from queuelib.queue import LifoDiskQueue

        total = 0
        root = Path(jobdir) / QUEUE_DIRNAME
        if not root.exists():
            self.sent = 0
            return 0
        for queue_file in sorted(root.iterdir()):
            if queue_file.is_file() and queue_file.name != "active.json":
                reader = LifoDiskQueue(str(queue_file))
                try:
                    total += len(reader)
                finally:
                    reader.close()
        self.sent = total
        return total
