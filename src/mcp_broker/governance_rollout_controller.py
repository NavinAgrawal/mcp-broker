"""Local rollout controller for governance simulation decisions."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


ROLLOUT_CONTROLLER_SCHEMA_VERSION = 1
LOCAL_SIMULATION_MODE = "local_simulation_only"
SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class GovernanceRolloutControllerError(ValueError):
    """Raised when rollout controller input cannot be converted to audit records."""


def control_rollout(
    *,
    simulation: Mapping[str, Any],
    state_dir: Path,
    operator: str,
    bundle: Mapping[str, Any],
    created_at: str | None = None,
) -> dict[str, object]:
    _validate_simulation(simulation)
    created = created_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    normalized_bundle = _bundle_metadata(bundle)
    records = _records_from_simulation(
        simulation=simulation,
        operator=_required_string(operator, "operator"),
        bundle=normalized_bundle,
        created_at=created,
    )
    rollout_dir = state_dir.expanduser() / "governance-rollout"
    actions_dir = rollout_dir / "actions"
    audit_log_path = rollout_dir / "action-log.jsonl"
    actions_dir.mkdir(parents=True, exist_ok=True)

    action_paths = []
    for record in records:
        action_path = actions_dir / f"{record['action_id']}.json"
        _write_json_new(action_path, record)
        action_paths.append(str(action_path))
    _append_jsonl(audit_log_path, records)
    return {
        "schema_version": ROLLOUT_CONTROLLER_SCHEMA_VERSION,
        "action": "rollout-control",
        "action_count": len(records),
        "action_paths": action_paths,
        "audit_log_path": str(audit_log_path),
        "changed_runtime_state": False,
    }


def _validate_simulation(simulation: Mapping[str, Any]) -> None:
    if simulation.get("mode") != LOCAL_SIMULATION_MODE:
        raise GovernanceRolloutControllerError(
            "rollout controller accepts only local simulation results"
        )
    if not str(simulation.get("state", "")).strip():
        raise GovernanceRolloutControllerError("simulation state is required")
    if not isinstance(simulation.get("decisions", []), list):
        raise GovernanceRolloutControllerError("simulation decisions must be a list")
    if not isinstance(simulation.get("reasons", []), list):
        raise GovernanceRolloutControllerError("simulation reasons must be a list")


def _records_from_simulation(
    *,
    simulation: Mapping[str, Any],
    operator: str,
    bundle: Mapping[str, Any],
    created_at: str,
) -> list[dict[str, object]]:
    state = str(simulation["state"])
    reasons = [str(reason) for reason in simulation.get("reasons", [])]
    if state in ("approval_required", "compatibility_rejection"):
        return [
            _record(
                index=1,
                broker_id="fleet",
                stage="fleet",
                action="hold",
                source_state=state,
                mode=LOCAL_SIMULATION_MODE,
                operator=operator,
                bundle=bundle,
                created_at=created_at,
                requires_approval=state == "approval_required",
                reasons=reasons,
                decision={},
            )
        ]

    decisions = [_mapping(decision) for decision in simulation.get("decisions", [])]
    if not decisions:
        raise GovernanceRolloutControllerError("simulation decisions are required")
    return [
        _record(
            index=index,
            broker_id=_required_string(decision.get("broker_id"), "broker_id"),
            stage=_required_string(decision.get("stage"), "stage"),
            action=_action_for_decision_state(
                _required_string(decision.get("state"), "decision state")
            ),
            source_state=state,
            mode=LOCAL_SIMULATION_MODE,
            operator=operator,
            bundle=bundle,
            created_at=created_at,
            requires_approval=False,
            reasons=reasons,
            decision=decision,
        )
        for index, decision in enumerate(decisions, start=1)
    ]


def _record(
    *,
    index: int,
    broker_id: str,
    stage: str,
    action: str,
    source_state: str,
    mode: str,
    operator: str,
    bundle: Mapping[str, Any],
    created_at: str,
    requires_approval: bool,
    reasons: list[str],
    decision: Mapping[str, Any],
) -> dict[str, object]:
    action_id = f"{index:04d}-{_safe_id_part(broker_id)}-{_safe_id_part(action)}"
    return {
        "schema_version": ROLLOUT_CONTROLLER_SCHEMA_VERSION,
        "action_id": action_id,
        "created_at": created_at,
        "operator": operator,
        "mode": mode,
        "source_state": source_state,
        "bundle": dict(bundle),
        "broker_id": broker_id,
        "stage": stage,
        "action": action,
        "requires_approval": requires_approval,
        "changed_runtime_state": False,
        "reasons": reasons,
        "decision": dict(decision),
    }


def _action_for_decision_state(state: str) -> str:
    if state == "canary":
        return "canary"
    if state == "staged_rollout":
        return "staged"
    if state == "broad_rollout":
        return "broad"
    if state == "rollback":
        return "rollback"
    raise GovernanceRolloutControllerError(f"unsupported decision state: {state}")


def _bundle_metadata(bundle: Mapping[str, Any]) -> dict[str, object]:
    digest = _mapping(bundle.get("digest"))
    return {
        "bundle_id": _required_string(bundle.get("bundle_id"), "bundle_id"),
        "version": _required_string(bundle.get("version"), "bundle_version"),
        "channel": _required_string(bundle.get("channel"), "bundle_channel"),
        "digest": {
            "algorithm": _required_string(digest.get("algorithm"), "digest algorithm"),
            "value": _required_string(digest.get("value"), "digest value"),
        },
    }


def _safe_id_part(value: str) -> str:
    sanitized = SAFE_ID_PATTERN.sub("-", value).strip("-")
    if not sanitized:
        raise GovernanceRolloutControllerError("empty action id component")
    return sanitized


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceRolloutControllerError(f"{field_name} is required")
    return value.strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_json_mapping(path: Path) -> dict[str, Any]:
    with path.expanduser().open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise GovernanceRolloutControllerError(f"expected JSON object: {path}")
    return loaded


def _write_json_new(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        raise GovernanceRolloutControllerError(f"rollout action already exists: {path}")
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _append_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _parse_digest(value: str) -> dict[str, str]:
    if ":" not in value:
        raise GovernanceRolloutControllerError("bundle digest must be algorithm:value")
    algorithm, digest_value = value.split(":", maxsplit=1)
    return {
        "algorithm": _required_string(algorithm, "digest algorithm"),
        "value": _required_string(digest_value, "digest value"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = control_rollout(
            simulation=_load_json_mapping(args.simulation),
            state_dir=args.state_dir,
            operator=args.operator,
            bundle={
                "bundle_id": args.bundle_id,
                "version": args.bundle_version,
                "channel": args.bundle_channel,
                "digest": _parse_digest(args.bundle_digest),
            },
            created_at=args.created_at,
        )
    except (GovernanceRolloutControllerError, OSError, json.JSONDecodeError) as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write(
        "governance rollout actions recorded: "
        f"{report['action_count']} record={report['audit_log_path']}\n"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record local rollout-control actions")
    parser.add_argument("--simulation", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--bundle-version", required=True)
    parser.add_argument("--bundle-channel", required=True)
    parser.add_argument("--bundle-digest", required=True)
    parser.add_argument("--created-at")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
