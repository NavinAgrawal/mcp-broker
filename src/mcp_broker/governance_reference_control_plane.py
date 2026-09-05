"""Local reference control-plane flow for governance contracts."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from mcp_broker.fleet_collection import (
    FleetCollectionError,
    prepare_collection_envelope,
)
from mcp_broker.governance_approval import GovernanceApprovalError, create_approval
from mcp_broker.governance_assignment import (
    GovernanceAssignmentError,
    evaluate_assignment,
)
from mcp_broker.governance_publish import GovernancePublishError, publish_bundle
from mcp_broker.governance_rollout_controller import (
    GovernanceRolloutControllerError,
    control_rollout,
)
from mcp_broker.rollout_simulator import simulate_rollout


REFERENCE_CONTROL_PLANE_SCHEMA_VERSION = 1
LOCAL_REFERENCE_MODE = "local_reference_only"
REFERENCE_DIR_NAME = "governance-reference-control-plane"
REFERENCE_CONTRACTS = (
    "publish",
    "assign",
    "collect",
    "rollout_control",
    "approve",
    "rollback",
)


class GovernanceReferenceControlPlaneError(ValueError):
    """Raised when the local reference control-plane flow is unsafe."""


def run_reference_control_plane(
    *,
    mode: str,
    state_dir: Path,
    bundle_path: Path,
    assignment_source: Mapping[str, Any],
    broker_context: Mapping[str, Any],
    fleet_statuses: Sequence[Mapping[str, Any]],
    target_url: str,
    auth_ref: str,
    operator: str,
    signature_ref: str,
    provenance: Mapping[str, str],
    approval_expires_at: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    if mode != LOCAL_REFERENCE_MODE:
        raise GovernanceReferenceControlPlaneError(
            f"reference control plane supports only {LOCAL_REFERENCE_MODE}"
        )
    created = created_at or _utc_now()
    control_dir = state_dir.expanduser() / REFERENCE_DIR_NAME
    try:
        bundle = _load_json_mapping(bundle_path)
        published_manifest_path = publish_bundle(
            bundle_path=bundle_path,
            output_dir=control_dir / "published",
            signature_ref=signature_ref,
            provenance=provenance,
            promotion_state="candidate",
        )
        published_manifest = _load_json_mapping(published_manifest_path)
        assignment = evaluate_assignment(
            assignment_source=assignment_source,
            published_manifests=[published_manifest],
            broker_context=broker_context,
        )
        collection = prepare_collection_envelope(
            _primary_fleet_status(fleet_statuses),
            target_url=target_url,
            auth_ref=auth_ref,
            retention_days=30,
            generated_at=created,
            collector_id=str(broker_context.get("broker_id", "reference-control-plane")),
        )
        simulation = simulate_rollout(
            bundle=_simulation_bundle(bundle=bundle, broker_context=broker_context),
            fleet_statuses=[dict(status) for status in fleet_statuses],
            approval_granted=True,
        )
        rollout_control = control_rollout(
            simulation=simulation,
            state_dir=state_dir,
            operator=operator,
            bundle=assignment["target"],
            created_at=created,
        )
        action_ids = _action_ids(rollout_control)
        approval = _create_rollout_or_rollback_approval(
            state_dir=state_dir,
            action_ids=action_ids,
            rollback_required=_has_rollback_action(rollout_control),
            operator=operator,
            approval_expires_at=approval_expires_at,
            created_at=created,
        )
    except (
        FleetCollectionError,
        GovernanceApprovalError,
        GovernanceAssignmentError,
        GovernancePublishError,
        GovernanceRolloutControllerError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise GovernanceReferenceControlPlaneError(str(exc)) from exc

    report_path = control_dir / "reports" / "reference-report.json"
    rollback = _rollback_summary(
        approval=approval,
        action_ids=action_ids,
        rollback_required=_has_rollback_action(rollout_control),
    )
    report = {
        "schema_version": REFERENCE_CONTROL_PLANE_SCHEMA_VERSION,
        "mode": LOCAL_REFERENCE_MODE,
        "contracts": list(REFERENCE_CONTRACTS),
        "created_at": created,
        "publish": {
            "manifest_path": str(published_manifest_path),
            "bundle": published_manifest["bundle"],
            "changed_runtime_state": False,
        },
        "assignment": assignment,
        "collection": collection,
        "rollout_simulation": simulation,
        "rollout_control": rollout_control,
        "approval": approval,
        "rollback": rollback,
        "changed_runtime_state": False,
        "report_path": str(report_path),
    }
    _write_json_atomic(report_path, report)
    return report


def _create_rollout_or_rollback_approval(
    *,
    state_dir: Path,
    action_ids: Sequence[str],
    rollback_required: bool,
    operator: str,
    approval_expires_at: str,
    created_at: str,
) -> dict[str, Any]:
    request_type = "rollback" if rollback_required else "rollout"
    reason = (
        "reference control-plane rollback approval"
        if rollback_required
        else "reference control-plane rollout approval"
    )
    return create_approval(
        state_dir=state_dir,
        request_type=request_type,
        operator=operator,
        reason=reason,
        expires_at=approval_expires_at,
        action_ids=action_ids,
        policy_paths=[],
        break_glass_record_id=None,
        created_at=created_at,
    )


def _rollback_summary(
    *,
    approval: Mapping[str, Any],
    action_ids: Sequence[str],
    rollback_required: bool,
) -> dict[str, Any]:
    if not rollback_required:
        return {
            "state": "not_required",
            "changed_runtime_state": False,
        }
    return {
        "state": "approval_recorded",
        "action_ids": list(action_ids),
        "approval_record_path": approval["record_path"],
        "changed_runtime_state": False,
    }


def _simulation_bundle(
    *,
    bundle: Mapping[str, Any],
    broker_context: Mapping[str, Any],
) -> dict[str, Any]:
    simulation_bundle = dict(bundle)
    broker_id = str(broker_context.get("broker_id", "reference-broker"))
    stage_name = str(broker_context.get("ring", "canary")) or "canary"
    simulation_bundle["rollout"] = {
        "rollback_on_statuses": ["degraded"],
        "stages": [
            {
                "name": stage_name,
                "broker_ids": [broker_id],
            }
        ],
    }
    return simulation_bundle


def _has_rollback_action(rollout_control: Mapping[str, Any]) -> bool:
    for path in rollout_control.get("action_paths", []):
        action = _load_json_mapping(Path(str(path)))
        if action.get("action") == "rollback":
            return True
    return False


def _action_ids(rollout_control: Mapping[str, Any]) -> list[str]:
    action_ids = []
    for path in rollout_control.get("action_paths", []):
        action = _load_json_mapping(Path(str(path)))
        action_ids.append(str(action["action_id"]))
    return action_ids


def _primary_fleet_status(fleet_statuses: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not fleet_statuses:
        raise GovernanceReferenceControlPlaneError("at least one fleet status is required")
    return fleet_statuses[0]


def _load_json_mapping(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.expanduser().read_bytes())
    if not isinstance(loaded, dict):
        raise GovernanceReferenceControlPlaneError(f"expected JSON object: {path}")
    return loaded


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_reference_control_plane(
            mode=args.mode,
            state_dir=args.state_dir,
            bundle_path=args.bundle,
            assignment_source=_load_json_mapping(args.assignment_source),
            broker_context=_load_json_mapping(args.broker_context),
            fleet_statuses=_load_fleet_statuses(args.fleet_status),
            target_url=args.target_url,
            auth_ref=args.auth_ref,
            operator=args.operator,
            signature_ref=args.signature_ref,
            provenance=_load_json_mapping(args.provenance),
            approval_expires_at=args.approval_expires_at,
            created_at=args.created_at,
        )
    except (GovernanceReferenceControlPlaneError, OSError, json.JSONDecodeError) as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write(
        "governance reference control-plane report: "
        f"{report['report_path']}\n"
    )
    return 0


def _load_fleet_statuses(path: Path) -> list[dict[str, Any]]:
    loaded = json.loads(path.expanduser().read_bytes())
    if isinstance(loaded, list):
        return [_mapping(item) for item in loaded]
    return [_mapping(loaded)]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local reference control-plane flow")
    parser.add_argument("--mode", default=LOCAL_REFERENCE_MODE)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--assignment-source", required=True, type=Path)
    parser.add_argument("--broker-context", required=True, type=Path)
    parser.add_argument("--fleet-status", required=True, type=Path)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--auth-ref", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--signature-ref", required=True)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--approval-expires-at", required=True)
    parser.add_argument("--created-at")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
