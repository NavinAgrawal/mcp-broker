"""Local break-glass audit records for policy bypass operations."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence


BREAK_GLASS_DIR_NAME = "break-glass"
POLICY_PATH_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


class BreakGlassError(ValueError):
    """Raised when a break-glass record is invalid or inactive."""


@dataclass(frozen=True)
class BreakGlassStore:
    state_dir: Path

    @property
    def break_glass_dir(self) -> Path:
        return self.state_dir.expanduser() / BREAK_GLASS_DIR_NAME

    @property
    def records_dir(self) -> Path:
        return self.break_glass_dir / "records"

    @property
    def active_pointer(self) -> Path:
        return self.break_glass_dir / "active.json"

    @property
    def audit_path(self) -> Path:
        return self.break_glass_dir / "audit.jsonl"

    def create(
        self,
        *,
        reason: str,
        operator: str,
        expires_at: str,
        bypassed_policy_paths: Sequence[str],
        created_at: str | None = None,
    ) -> dict[str, object]:
        created_at = created_at or _utc_now()
        clean_reason = _required_text(reason, "reason")
        clean_operator = _required_text(operator, "operator")
        clean_paths = _policy_paths(bypassed_policy_paths)
        _require_future_expiration(expires_at, now=created_at)
        record_seed = {
            "bypassed_policy_paths": clean_paths,
            "created_at": created_at,
            "expires_at": expires_at,
            "operator": clean_operator,
            "reason": clean_reason,
        }
        record_id = _record_id(record_seed)
        record_path = self.records_dir / f"{record_id}.json"
        record = {
            **record_seed,
            "audit_path": str(self.audit_path),
            "record_id": record_id,
            "status": "active",
        }
        _write_json_atomic(record_path, record)
        _write_json_atomic(
            self.active_pointer,
            {"record_id": record_id, "record_path": str(record_path)},
        )
        self._append_audit(
            {
                "event": "break_glass.created",
                "record_id": record_id,
                "operator": clean_operator,
                "reason": clean_reason,
                "bypassed_policy_paths": clean_paths,
                "expires_at": expires_at,
            },
            ts=created_at,
        )
        return record

    def status(self, *, now: str | None = None) -> dict[str, object]:
        active_record = self._active_record_or_none()
        if active_record is None or _is_expired(str(active_record["expires_at"]), now=now):
            return {"active_record": None, "degraded": False, "status": "inactive"}
        return {"active_record": active_record, "degraded": True, "status": "active"}

    def require_active_record(self, *, now: str | None = None) -> dict[str, object]:
        active_record = self._active_record_or_none()
        if active_record is None:
            raise BreakGlassError("break-glass record is not active")
        if _is_expired(str(active_record["expires_at"]), now=now):
            raise BreakGlassError("break-glass record expired")
        return active_record

    def _active_record_or_none(self) -> dict[str, object] | None:
        if not self.active_pointer.exists():
            return None
        pointer = _read_json(self.active_pointer)
        record = _read_json(Path(str(pointer["record_path"])))
        if str(record["record_id"]) != str(pointer["record_id"]):
            raise BreakGlassError("break-glass active pointer does not match record")
        return record

    def _append_audit(self, entry: dict[str, object], *, ts: str) -> None:
        self.break_glass_dir.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({**entry, "ts": ts}, sort_keys=True, separators=(",", ":")) + "\n"
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    store = BreakGlassStore(args.state_dir)
    try:
        if args.break_glass_command == "create":
            result = store.create(
                reason=args.reason,
                operator=args.operator,
                expires_at=args.expires_at,
                bypassed_policy_paths=args.bypass_policy,
            )
        elif args.break_glass_command == "status":
            result = store.status()
        else:
            raise BreakGlassError(f"unknown break-glass command: {args.break_glass_command}")
    except (BreakGlassError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage local break-glass audit records")
    subparsers = parser.add_subparsers(dest="break_glass_command", required=True)
    create = subparsers.add_parser("create", help="Create an expiring break-glass audit record")
    create.add_argument("--state-dir", required=True, type=Path)
    create.add_argument("--reason", required=True)
    create.add_argument("--operator", required=True)
    create.add_argument("--expires-at", required=True)
    create.add_argument("--bypass-policy", action="append", default=[], required=True)
    status = subparsers.add_parser("status", help="Report active break-glass status")
    status.add_argument("--state-dir", required=True, type=Path)
    return parser.parse_args(argv)


def _required_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise BreakGlassError(f"{label} is required")
    return clean


def _policy_paths(values: Sequence[str]) -> list[str]:
    paths = [value.strip() for value in values if value.strip()]
    if not paths:
        raise BreakGlassError("at least one bypassed policy path is required")
    invalid = [value for value in paths if POLICY_PATH_PATTERN.fullmatch(value) is None]
    if invalid:
        raise BreakGlassError(f"invalid bypassed policy path: {invalid[0]}")
    return paths


def _record_id(record_seed: dict[str, object]) -> str:
    payload = json.dumps(record_seed, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"break-glass-{digest}"


def _require_future_expiration(expires_at: str, *, now: str) -> None:
    if _parse_timestamp(expires_at) <= _parse_timestamp(now):
        raise BreakGlassError("expires_at must be in the future")


def _is_expired(expires_at: str, *, now: str | None) -> bool:
    return _parse_timestamp(expires_at) <= _parse_timestamp(now or _utc_now())


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BreakGlassError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise BreakGlassError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise BreakGlassError(f"expected JSON object: {path}")
    return loaded


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
