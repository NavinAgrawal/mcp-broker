"""Local deterministic adapter for shared-runtime deployment state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Mapping

from mcp_broker.shared_runtime_policy import (
    SharedRuntimePolicyError,
    validate_tenant_context,
)


class DistributedStateError(ValueError):
    """Raised when distributed state input is unsafe."""


class DistributedStateConflict(DistributedStateError):
    """Raised when a state mutation conflicts with current state."""


@dataclass(frozen=True)
class DistributedStateStore:
    state_dir: Path

    @property
    def root(self) -> Path:
        return self.state_dir.expanduser() / "shared-runtime"

    @property
    def lock_path(self) -> Path:
        return self.root / "lock.json"

    @property
    def lock_counter_path(self) -> Path:
        return self.root / "lock-counter.json"

    @property
    def active_path(self) -> Path:
        return self.root / "active.json"

    @property
    def previous_path(self) -> Path:
        return self.root / "previous.json"

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def acquire_lock(
        self,
        *,
        owner_id: str,
        tenant_context: Mapping[str, Any],
        now: datetime,
        ttl_seconds: int,
    ) -> dict[str, str]:
        context = _tenant_context(tenant_context)
        safe_owner_id = _required_identifier(owner_id, "owner_id")
        safe_now = _utc_datetime(now)
        if ttl_seconds <= 0:
            raise DistributedStateError("ttl_seconds must be positive")
        existing = _read_json_optional(self.lock_path)
        if existing is not None:
            expires_at = _parse_utc(str(existing["expires_at"]))
            if expires_at > safe_now:
                self._append_audit(
                    _lock_event(
                        context=context,
                        owner_id=safe_owner_id,
                        result="denied",
                        denial_reason="lock_conflict",
                    )
                )
                raise DistributedStateConflict("state lock is held")
            self._append_audit(
                _lock_event(
                    context=context,
                    owner_id=safe_owner_id,
                    result="recovered",
                    stale_owner_id=str(existing["owner_id"]),
                    stale_token=str(existing["token"]),
                )
            )
        token = self._next_lock_token()
        lock = {
            "owner_id": safe_owner_id,
            "token": token,
            "tenant_id": context["tenant_id"],
            "workspace_id": context["workspace_id"],
            "user_id": context["user_id"],
            "acquired_at": _format_utc(safe_now),
            "expires_at": _format_utc(safe_now + timedelta(seconds=ttl_seconds)),
        }
        _write_json_atomic(self.lock_path, lock)
        self._append_audit(
            _lock_event(context=context, owner_id=safe_owner_id, result="acquired")
        )
        return lock

    def apply_state(
        self,
        *,
        lock: Mapping[str, str],
        state: Mapping[str, Any],
        expected_active_revision: int | None,
    ) -> dict[str, Any]:
        self._require_matching_lock(lock)
        active = _read_json_optional(self.active_path)
        active_revision = _revision(active)
        if active_revision != expected_active_revision:
            self._append_audit(
                _mutation_event(
                    event_type="distributed_state_apply",
                    lock=lock,
                    result="denied",
                    denial_reason="revision_conflict",
                )
            )
            raise DistributedStateConflict("active revision conflict")
        record = {
            "revision": self._next_revision(active_revision),
            "status": "active",
            "state": dict(sorted(state.items())),
        }
        if active is not None:
            _write_json_atomic(self.previous_path, active)
        _write_json_atomic(self.active_path, record)
        self._append_journal({"action": "apply", "record": record, "previous": active})
        self._append_audit(
            _mutation_event(
                event_type="distributed_state_apply",
                lock=lock,
                result="allowed",
                revision=record["revision"],
            )
        )
        return record

    def rollback(self, *, lock: Mapping[str, str]) -> dict[str, int]:
        self._require_matching_lock(lock)
        active = _require_json(self.active_path, "active shared-runtime state is missing")
        previous = _require_json(
            self.previous_path,
            "previous shared-runtime state is missing",
        )
        _write_json_atomic(self.active_path, previous)
        _write_json_atomic(self.previous_path, active)
        self._append_journal(
            {
                "action": "rollback",
                "active_record": previous,
                "previous_record": active,
            }
        )
        self._append_audit(
            _mutation_event(
                event_type="distributed_state_rollback",
                lock=lock,
                result="allowed",
                revision=int(previous["revision"]),
            )
        )
        return {
            "active_revision": int(previous["revision"]),
            "previous_revision": int(active["revision"]),
        }

    def recover(self) -> dict[str, object]:
        removed = self._remove_partial_files()
        if self.active_path.exists():
            active = _require_json(self.active_path, "active shared-runtime state is missing")
            self._append_audit(
                {
                    "event_type": "distributed_state_recovery",
                    "result": "checked",
                    "active_revision": int(active["revision"]),
                    "removed_partial_files": removed,
                }
            )
            return {
                "active_revision": int(active["revision"]),
                "replayed": False,
                "removed_partial_files": removed,
            }
        active, previous = self._replay_journal()
        if active is None:
            raise DistributedStateError("no shared-runtime journal entries to recover")
        _write_json_atomic(self.active_path, active)
        if previous is not None:
            _write_json_atomic(self.previous_path, previous)
        self._append_audit(
            {
                "event_type": "distributed_state_recovery",
                "result": "replayed",
                "active_revision": int(active["revision"]),
                "removed_partial_files": removed,
            }
        )
        return {
            "active_revision": int(active["revision"]),
            "replayed": True,
            "removed_partial_files": removed,
        }

    def _require_matching_lock(self, lock: Mapping[str, str]) -> None:
        current = _require_json(self.lock_path, "shared-runtime state lock is missing")
        if current.get("owner_id") != lock.get("owner_id") or current.get("token") != lock.get("token"):
            self._append_audit(
                _mutation_event(
                    event_type="distributed_state_mutation",
                    lock=lock,
                    result="denied",
                    denial_reason="lock_token_mismatch",
                )
            )
            raise DistributedStateConflict("state mutation requires matching lock token")

    def _next_lock_token(self) -> str:
        current = _read_json_optional(self.lock_counter_path)
        next_value = int(current["value"]) + 1 if current is not None else 1
        _write_json_atomic(self.lock_counter_path, {"value": next_value})
        return f"{next_value:012d}"

    def _next_revision(self, active_revision: int | None) -> int:
        if active_revision is not None:
            return active_revision + 1
        latest = 0
        if self.journal_path.exists():
            for entry in _read_jsonl(self.journal_path):
                for key in ("record", "active_record", "previous_record"):
                    record = entry.get(key)
                    if isinstance(record, Mapping):
                        latest = max(latest, int(record["revision"]))
        return latest + 1

    def _append_journal(self, entry: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), sort_keys=True, separators=(",", ":")) + "\n")

    def _append_audit(self, entry: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), sort_keys=True, separators=(",", ":")) + "\n")

    def _remove_partial_files(self) -> list[str]:
        if not self.root.exists():
            return []
        removed: list[str] = []
        for partial in sorted(self.root.rglob("*.tmp")):
            removed.append(str(partial))
            partial.unlink()
        return removed

    def _replay_journal(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        active: dict[str, Any] | None = None
        previous: dict[str, Any] | None = None
        if not self.journal_path.exists():
            return active, previous
        for entry in _read_jsonl(self.journal_path):
            action = entry.get("action")
            if action == "apply":
                record = entry.get("record")
                if not isinstance(record, dict):
                    raise DistributedStateError("apply journal entry is missing record")
                previous = active
                active = record
            elif action == "rollback":
                active_record = entry.get("active_record")
                previous_record = entry.get("previous_record")
                if not isinstance(active_record, dict) or not isinstance(previous_record, dict):
                    raise DistributedStateError("rollback journal entry is incomplete")
                active = active_record
                previous = previous_record
        return active, previous


def _lock_event(
    *,
    context: Mapping[str, str],
    owner_id: str,
    result: str,
    denial_reason: str | None = None,
    stale_owner_id: str | None = None,
    stale_token: str | None = None,
) -> dict[str, str]:
    event = {
        "event_type": "distributed_state_lock",
        "tenant_id": context["tenant_id"],
        "workspace_id": context["workspace_id"],
        "user_id": context["user_id"],
        "owner_id": owner_id,
        "result": result,
    }
    if denial_reason is not None:
        event["denial_reason"] = denial_reason
    if stale_owner_id is not None:
        event["stale_owner_id"] = stale_owner_id
    if stale_token is not None:
        event["stale_token"] = stale_token
    return event


def _mutation_event(
    *,
    event_type: str,
    lock: Mapping[str, str],
    result: str,
    revision: int | None = None,
    denial_reason: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": event_type,
        "tenant_id": lock.get("tenant_id", ""),
        "workspace_id": lock.get("workspace_id", ""),
        "user_id": lock.get("user_id", ""),
        "owner_id": lock.get("owner_id", ""),
        "lock_token": lock.get("token", ""),
        "result": result,
    }
    if revision is not None:
        event["revision"] = revision
    if denial_reason is not None:
        event["denial_reason"] = denial_reason
    return event


def _tenant_context(context: Mapping[str, Any]) -> dict[str, str]:
    try:
        return validate_tenant_context(context)
    except SharedRuntimePolicyError as error:
        raise DistributedStateError(str(error)) from error


def _required_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DistributedStateError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise DistributedStateError(f"{field} must not contain path separators")
    return value


def _revision(record: Mapping[str, Any] | None) -> int | None:
    if record is None:
        return None
    return int(record["revision"])


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DistributedStateError("now must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _read_json_optional(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _require_json(path, f"expected JSON object: {path}")


def _require_json(path: Path, message: str) -> dict[str, Any]:
    if not path.exists():
        raise DistributedStateError(message)
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise DistributedStateError(f"expected JSON object: {path}")
    return loaded


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        loaded = json.loads(line)
        if not isinstance(loaded, dict):
            raise DistributedStateError(f"expected JSON object in {path}")
        entries.append(loaded)
    return entries


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)
