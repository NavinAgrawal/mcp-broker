"""Deterministic local rollout simulation for governance bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence


_LOCAL_SIMULATION_MODE = "local_simulation_only"


def simulate_rollout(
    *,
    bundle: dict[str, Any],
    fleet_statuses: Sequence[dict[str, Any]],
    approval_granted: bool,
) -> dict[str, Any]:
    compatibility_reasons = _compatibility_rejections(bundle, fleet_statuses)
    if compatibility_reasons:
        return _result("compatibility_rejection", reasons=compatibility_reasons)

    rollback_reasons, rollback_decisions = _rollback_decisions(bundle, fleet_statuses)
    if rollback_reasons:
        return _result("rollback", decisions=rollback_decisions, reasons=rollback_reasons)

    if _policy(bundle).get("approval_required") is True and not approval_granted:
        return _result(
            "approval_required",
            reasons=["policy approval_required is true and approval was not granted"],
        )

    return _result("ready", decisions=_rollout_decisions(bundle, fleet_statuses))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = json.loads(args.bundle.expanduser().read_bytes())
    fleet_statuses = _load_fleet_statuses(args.fleet_status.expanduser())
    sys.stdout.write(
        json.dumps(
            simulate_rollout(
                bundle=bundle,
                fleet_statuses=fleet_statuses,
                approval_granted=args.approved,
            ),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simulate a local governance rollout")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--fleet-status", required=True, type=Path)
    parser.add_argument("--approved", action="store_true")
    return parser


def _load_fleet_statuses(path: Path) -> list[dict[str, Any]]:
    loaded = json.loads(path.read_bytes())
    if isinstance(loaded, list):
        return [_mapping(item) for item in loaded]
    return [_mapping(loaded)]


def _compatibility_rejections(
    bundle: dict[str, Any],
    fleet_statuses: Sequence[dict[str, Any]],
) -> list[str]:
    compatibility = _mapping(bundle.get("compatibility"))
    min_version = _int_or_none(compatibility.get("min_config_schema_version"))
    max_version = _int_or_none(compatibility.get("max_config_schema_version"))
    allowed_environments = set(_list(_mapping(bundle.get("applies_to")).get("environments")))
    allowed_brokers = set(_list(_mapping(bundle.get("applies_to")).get("broker_ids")))
    reasons: list[str] = []
    for status in fleet_statuses:
        identity = _identity(status)
        broker_id = _broker_id(identity)
        schema_version = _int_or_none(identity.get("schema_version"))
        if schema_version is not None and not _in_range(schema_version, min_version, max_version):
            reasons.append(
                f"{broker_id} config schema version {schema_version} "
                f"outside supported range {min_version}..{max_version}"
            )
        environment = identity.get("environment")
        if allowed_environments and environment not in allowed_environments:
            reasons.append(f"{broker_id} environment {environment!r} is not targeted")
        if allowed_brokers and broker_id not in allowed_brokers:
            reasons.append(f"{broker_id} is not targeted")
    return reasons


def _rollback_decisions(
    bundle: dict[str, Any],
    fleet_statuses: Sequence[dict[str, Any]],
) -> tuple[list[str], list[dict[str, str]]]:
    rollback_statuses = set(_list(_rollout(bundle).get("rollback_on_statuses")))
    reasons: list[str] = []
    decisions: list[dict[str, str]] = []
    for status in fleet_statuses:
        health_status = str(_mapping(status.get("health")).get("status", "unknown"))
        if health_status in rollback_statuses:
            broker_id = _broker_id(_identity(status))
            reasons.append(f"{broker_id} health status {health_status} triggers rollback")
            decisions.append(
                {
                    "broker_id": broker_id,
                    "stage": _stage_name_for_broker(bundle, broker_id),
                    "state": "rollback",
                }
            )
    return reasons, decisions


def _rollout_decisions(
    bundle: dict[str, Any],
    fleet_statuses: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    status_by_broker = {
        _broker_id(_identity(status)): status for status in fleet_statuses
    }
    decisions: list[dict[str, str]] = []
    for stage in _stages(bundle):
        stage_name = str(stage.get("name", "staged"))
        for broker_id in _list(stage.get("broker_ids")):
            if broker_id in status_by_broker:
                decisions.append(
                    {
                        "broker_id": broker_id,
                        "stage": stage_name,
                        "state": _state_for_stage(stage_name),
                    }
                )
    return decisions


def _state_for_stage(stage_name: str) -> str:
    if stage_name == "canary":
        return "canary"
    if stage_name == "broad":
        return "broad_rollout"
    return "staged_rollout"


def _stage_name_for_broker(bundle: dict[str, Any], broker_id: str) -> str:
    for stage in _stages(bundle):
        if broker_id in _list(stage.get("broker_ids")):
            return str(stage.get("name", "staged"))
    return "unassigned"


def _stages(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [_mapping(stage) for stage in _list(_rollout(bundle).get("stages"))]


def _policy(bundle: dict[str, Any]) -> dict[str, Any]:
    return _mapping(bundle.get("policy"))


def _rollout(bundle: dict[str, Any]) -> dict[str, Any]:
    return _mapping(bundle.get("rollout"))


def _identity(status: dict[str, Any]) -> dict[str, Any]:
    return _mapping(status.get("identity"))


def _broker_id(identity: dict[str, Any]) -> str:
    value = identity.get("broker_id")
    return value if isinstance(value, str) and value else "unknown"


def _result(
    state: str,
    *,
    decisions: list[dict[str, str]] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": _LOCAL_SIMULATION_MODE,
        "state": state,
        "decisions": decisions or [],
        "reasons": reasons or [],
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _in_range(value: int, minimum: int | None, maximum: int | None) -> bool:
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
