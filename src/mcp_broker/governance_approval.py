"""Local operator approval records for governance mutations."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


APPROVAL_SCHEMA_VERSION = 1
APPROVAL_DIR_NAME = "governance-approvals"
REQUEST_TYPES = frozenset(("rollout", "rollback", "policy_override", "break_glass"))
POLICY_PATH_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class GovernanceApprovalError(ValueError):
    """Raised when a governance approval record is invalid."""


def create_approval(
    *,
    state_dir: Path,
    request_type: str,
    operator: str,
    reason: str,
    expires_at: str,
    action_ids: Sequence[str],
    policy_paths: Sequence[str],
    break_glass_record_id: str | None,
    created_at: str | None = None,
) -> dict[str, object]:
    created = created_at or _utc_now()
    clean_request_type = _request_type(request_type)
    clean_operator = _required_text(operator, "operator")
    clean_reason = _required_text(reason, "reason")
    _require_future_expiration(expires_at, now=created)
    target = _target_for_request(
        request_type=clean_request_type,
        action_ids=action_ids,
        policy_paths=policy_paths,
        break_glass_record_id=break_glass_record_id,
    )
    seed = {
        "approved": True,
        "created_at": created,
        "expires_at": expires_at,
        "operator": clean_operator,
        "reason": clean_reason,
        "request_type": clean_request_type,
        "target": target,
    }
    approval_id = _approval_id(seed)
    approval_dir = state_dir.expanduser() / APPROVAL_DIR_NAME
    records_dir = approval_dir / "records"
    audit_path = approval_dir / "audit.jsonl"
    record_path = records_dir / f"{approval_id}.json"
    record = {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "approved": True,
        "created_at": created,
        "expires_at": expires_at,
        "operator": clean_operator,
        "reason": clean_reason,
        "request_type": clean_request_type,
        "target": target,
        "requires_apply_step": True,
        "changed_runtime_state": False,
        "audit_path": str(audit_path),
    }
    _write_json_new(record_path, record)
    _append_audit(
        audit_path,
        {
            "approval_id": approval_id,
            "event": "governance_approval.created",
            "operator": clean_operator,
            "request_type": clean_request_type,
            "target": target,
            "ts": created,
        },
    )
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "approved": True,
        "request_type": clean_request_type,
        "record_path": str(record_path),
        "audit_path": str(audit_path),
        "requires_apply_step": True,
        "changed_runtime_state": False,
    }


def _target_for_request(
    *,
    request_type: str,
    action_ids: Sequence[str],
    policy_paths: Sequence[str],
    break_glass_record_id: str | None,
) -> dict[str, object]:
    if request_type in ("rollout", "rollback"):
        clean_action_ids = _safe_ids(action_ids, label="action id")
        if not clean_action_ids:
            raise GovernanceApprovalError("at least one action id is required")
        return {"action_ids": clean_action_ids}
    if request_type == "policy_override":
        clean_policy_paths = _policy_paths(policy_paths)
        return {"policy_paths": clean_policy_paths}
    if request_type == "break_glass":
        return {
            "break_glass_record_id": _safe_id(
                _required_text(break_glass_record_id or "", "break_glass_record_id"),
                label="break_glass_record_id",
            )
        }
    raise GovernanceApprovalError(f"unsupported request_type: {request_type}")


def _request_type(value: str) -> str:
    clean = value.strip()
    if clean not in REQUEST_TYPES:
        raise GovernanceApprovalError(f"unsupported request_type: {value}")
    return clean


def _safe_ids(values: Sequence[str], *, label: str) -> list[str]:
    return [_safe_id(value.strip(), label=label) for value in values if value.strip()]


def _safe_id(value: str, *, label: str) -> str:
    if SAFE_ID_PATTERN.fullmatch(value) is None:
        raise GovernanceApprovalError(f"invalid {label}: {value}")
    return value


def _policy_paths(values: Sequence[str]) -> list[str]:
    paths = [value.strip() for value in values if value.strip()]
    if not paths:
        raise GovernanceApprovalError("at least one policy path is required")
    invalid = [value for value in paths if POLICY_PATH_PATTERN.fullmatch(value) is None]
    if invalid:
        raise GovernanceApprovalError(f"invalid policy path: {invalid[0]}")
    return paths


def _required_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise GovernanceApprovalError(f"{label} is required")
    return clean


def _approval_id(record_seed: Mapping[str, object]) -> str:
    payload = json.dumps(record_seed, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"governance-approval-{digest}"


def _require_future_expiration(expires_at: str, *, now: str) -> None:
    if _parse_timestamp(expires_at) <= _parse_timestamp(now):
        raise GovernanceApprovalError("expires_at must be in the future")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernanceApprovalError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise GovernanceApprovalError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _write_json_new(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise GovernanceApprovalError(f"approval record already exists: {path}")
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _append_audit(path: Path, entry: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = create_approval(
            state_dir=args.state_dir,
            request_type=args.request_type,
            operator=args.operator,
            reason=args.reason,
            expires_at=args.expires_at,
            action_ids=args.action_id,
            policy_paths=args.policy_path,
            break_glass_record_id=args.break_glass_record_id,
            created_at=args.created_at,
        )
    except (GovernanceApprovalError, OSError, json.JSONDecodeError) as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write(
        "governance approval recorded: "
        f"{report['approval_id']} record={report['record_path']}\n"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record local governance approval")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--request-type", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--action-id", action="append", default=[])
    parser.add_argument("--policy-path", action="append", default=[])
    parser.add_argument("--break-glass-record-id")
    parser.add_argument("--created-at")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
