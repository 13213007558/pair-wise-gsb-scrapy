from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class PolicyError(Exception):
    """Raised when the local policy file cannot be trusted or parsed."""


@dataclass(frozen=True)
class _ScopeRule:
    host: str
    path_prefix: str
    scope: str

    def covers(self, url: str, scope: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path or "/"
        return (
            host == self.host
            and path.startswith(self.path_prefix)
            and scope == self.scope
        )


@dataclass(frozen=True)
class Policy:
    version: int
    issued_at: str
    allow: tuple[_ScopeRule, ...]
    deny: tuple[_ScopeRule, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Policy:
        try:
            version = raw["version"]
            issued_at = raw["issued_at"]
            allow = tuple(_rule(entry) for entry in raw["allow"])
            deny = tuple(_rule(entry) for entry in raw["deny"])
        except (KeyError, TypeError) as exc:
            raise PolicyError(f"policy field missing or malformed: {exc}") from exc
        if not isinstance(version, int) or isinstance(version, bool):
            raise PolicyError("policy version must be an integer")
        if not isinstance(issued_at, str) or not issued_at:
            raise PolicyError("policy issued_at must be a non-empty string")
        return cls(version=version, issued_at=issued_at, allow=allow, deny=deny)

    def is_revoked(self, url: str, scope: str) -> bool:
        # Explicit deny always wins, so a scope can be withdrawn without
        # editing every allow rule that previously granted it.
        if any(rule.covers(url, scope) for rule in self.deny):
            return True
        return not any(rule.covers(url, scope) for rule in self.allow)


def _rule(entry: Any) -> _ScopeRule:
    if not isinstance(entry, dict):
        raise PolicyError("scope rules must be objects")
    host = entry["host"]
    path_prefix = entry["path_prefix"]
    scope = entry["scope"]
    if not isinstance(host, str) or not host:
        raise PolicyError("scope rule host must be a non-empty string")
    if not isinstance(path_prefix, str) or not path_prefix.startswith("/"):
        raise PolicyError("scope rule path_prefix must start with '/'")
    if not isinstance(scope, str) or not scope:
        raise PolicyError("scope rule scope must be a non-empty string")
    return _ScopeRule(host=host, path_prefix=path_prefix, scope=scope)


def load_policy(path: str | Path) -> Policy:
    policy_path = Path(path)
    try:
        raw_text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"policy file unreadable: {type(exc).__name__}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"policy file is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy root must be an object")
    return Policy.from_dict(raw)


def fingerprint(text: str) -> str:
    """Return a stable, non-reversible identifier for audit output."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def mask_host(host: str) -> str:
    if len(host) <= 3:
        return "***"
    return host[:2] + "***" + host[-1:]


def mask_url(url: str) -> str:
    """Keep scheme and host visible, hide everything that may identify a path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{mask_host(parts.hostname or '')}/{fingerprint(url)}"
