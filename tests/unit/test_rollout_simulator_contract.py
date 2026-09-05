from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_broker.cli import main as cli_main
from tests.support.bundles import signed_bundle


pytestmark = [pytest.mark.unit]


def _bundle() -> dict[str, object]:
    bundle = signed_bundle()
    bundle["applies_to"] = {
        "broker_ids": ["broker-a", "broker-b", "broker-c"],
        "environments": ["local"],
    }
    bundle["policy"]["approval_required"] = True
    bundle["rollout"] = {
        "rollback_on_statuses": ["degraded", "failed"],
        "stages": [
            {"name": "canary", "broker_ids": ["broker-a"]},
            {"name": "staged", "broker_ids": ["broker-b"]},
            {"name": "broad", "broker_ids": ["broker-c"]},
        ],
    }
    return bundle


def _fleet_status(
    broker_id: str,
    *,
    status: str = "running",
    schema_version: int = 1,
) -> dict[str, object]:
    return {
        "identity": {
            "active_profiles": ["codex"],
            "broker_id": broker_id,
            "bundle_version": "unbundled",
            "environment": "local",
            "schema_version": schema_version,
        },
        "health": {
            "last_request_status": "ok",
            "started_at": "2026-07-01T12:00:00+00:00",
            "status": status,
            "updated_at": "2026-07-01T12:03:00+00:00",
        },
        "request_counters": {
            "request_errors_total": 0,
            "requests_total": 10,
        },
        "upstreams": {},
    }


def test_rollout_simulator_requires_approval_before_staging() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[
            _fleet_status("broker-a"),
            _fleet_status("broker-b"),
            _fleet_status("broker-c"),
        ],
        approval_granted=False,
    )

    assert result == {
        "mode": "local_simulation_only",
        "state": "approval_required",
        "decisions": [],
        "reasons": ["policy approval_required is true and approval was not granted"],
    }


def test_rollout_simulator_plans_canary_staged_and_broad_rollout() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[
            _fleet_status("broker-a"),
            _fleet_status("broker-b"),
            _fleet_status("broker-c"),
        ],
        approval_granted=True,
    )

    assert result == {
        "mode": "local_simulation_only",
        "state": "ready",
        "decisions": [
            {"broker_id": "broker-a", "stage": "canary", "state": "canary"},
            {"broker_id": "broker-b", "stage": "staged", "state": "staged_rollout"},
            {"broker_id": "broker-c", "stage": "broad", "state": "broad_rollout"},
        ],
        "reasons": [],
    }


def test_rollout_simulator_rejects_incompatible_broker() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[_fleet_status("broker-a", schema_version=2)],
        approval_granted=True,
    )

    assert result == {
        "mode": "local_simulation_only",
        "state": "compatibility_rejection",
        "decisions": [],
        "reasons": ["broker-a config schema version 2 outside supported range 1..1"],
    }


def test_rollout_simulator_rejects_schema_below_minimum() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[_fleet_status("broker-a", schema_version=0)],
        approval_granted=True,
    )

    assert result["reasons"] == [
        "broker-a config schema version 0 outside supported range 1..1"
    ]


def test_rollout_simulator_rejects_untargeted_environment_and_broker() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[
            {
                **_fleet_status("broker-z"),
                "identity": {
                    **_fleet_status("broker-z")["identity"],
                    "environment": "prod",
                },
            }
        ],
        approval_granted=True,
    )

    assert result == {
        "mode": "local_simulation_only",
        "state": "compatibility_rejection",
        "decisions": [],
        "reasons": [
            "broker-z environment 'prod' is not targeted",
            "broker-z is not targeted",
        ],
    }


def test_rollout_simulator_requests_rollback_on_unhealthy_broker() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    result = simulate_rollout(
        bundle=_bundle(),
        fleet_statuses=[_fleet_status("broker-a", status="degraded")],
        approval_granted=True,
    )

    assert result == {
        "mode": "local_simulation_only",
        "state": "rollback",
        "decisions": [
            {"broker_id": "broker-a", "stage": "canary", "state": "rollback"}
        ],
        "reasons": ["broker-a health status degraded triggers rollback"],
    }


def test_rollout_simulator_rollback_uses_unassigned_stage_for_unlisted_broker() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    bundle = _bundle()
    bundle["applies_to"]["broker_ids"].append("broker-d")

    result = simulate_rollout(
        bundle=bundle,
        fleet_statuses=[_fleet_status("broker-d", status="failed")],
        approval_granted=True,
    )

    assert result["state"] == "rollback"
    assert result["decisions"] == [
        {"broker_id": "broker-d", "stage": "unassigned", "state": "rollback"}
    ]


def test_rollout_simulator_uses_default_stage_name_for_malformed_stage() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    bundle = _bundle()
    bundle["rollout"]["stages"] = [{"broker_ids": ["broker-a"]}]

    result = simulate_rollout(
        bundle=bundle,
        fleet_statuses=[_fleet_status("broker-a")],
        approval_granted=True,
    )

    assert result["decisions"] == [
        {"broker_id": "broker-a", "stage": "staged", "state": "staged_rollout"}
    ]


def test_rollout_simulator_rolls_back_unknown_health_with_default_stage() -> None:
    from mcp_broker.rollout_simulator import simulate_rollout

    bundle = _bundle()
    bundle["rollout"]["rollback_on_statuses"].append("unknown")
    bundle["rollout"]["stages"] = [{"broker_ids": ["broker-a"]}]
    fleet_status = _fleet_status("broker-a")
    fleet_status.pop("health")

    assert simulate_rollout(
        bundle=bundle,
        fleet_statuses=[fleet_status],
        approval_granted=True,
    ) == {
        "mode": "local_simulation_only",
        "state": "rollback",
        "decisions": [
            {"broker_id": "broker-a", "stage": "staged", "state": "rollback"}
        ],
        "reasons": ["broker-a health status unknown triggers rollback"],
    }


def test_rollout_simulator_cli_outputs_local_simulation_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = tmp_path / "bundle.json"
    fleet_path = tmp_path / "fleet.json"
    bundle_path.write_text(json.dumps(_bundle()), encoding="utf-8")
    fleet_path.write_text(json.dumps([_fleet_status("broker-a")]), encoding="utf-8")

    assert (
        cli_main(
            [
                "rollout",
                "simulate",
                "--bundle",
                str(bundle_path),
                "--fleet-status",
                str(fleet_path),
                "--approved",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "local_simulation_only"
    assert payload["decisions"] == [
        {"broker_id": "broker-a", "stage": "canary", "state": "canary"}
    ]


def test_rollout_simulator_direct_cli_accepts_single_fleet_status_object(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.rollout_simulator import main

    bundle_path = tmp_path / "bundle.json"
    fleet_path = tmp_path / "fleet.json"
    bundle_path.write_text(json.dumps(_bundle()), encoding="utf-8")
    fleet_path.write_text(json.dumps(_fleet_status("broker-a")), encoding="utf-8")

    assert (
        main(
            [
                "--bundle",
                str(bundle_path),
                "--fleet-status",
                str(fleet_path),
                "--approved",
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["decisions"] == [
        {"broker_id": "broker-a", "stage": "canary", "state": "canary"}
    ]
    assert stdout == json.dumps(payload, sort_keys=True) + "\n"


def test_rollout_simulator_parser_has_exact_public_contract() -> None:
    from mcp_broker.rollout_simulator import _parser

    parser = _parser()
    assert parser.description == "Simulate a local governance rollout"
    actions = {action.dest: action for action in parser._actions}
    assert set(actions) == {"help", "bundle", "fleet_status", "approved"}
    assert actions["bundle"].required is True
    assert actions["bundle"].type is Path
    assert actions["fleet_status"].required is True
    assert actions["fleet_status"].type is Path
    assert actions["approved"].const is True
    assert actions["approved"].default is False


def test_rollout_simulator_loads_utf8_json_bytes(tmp_path: Path) -> None:
    from mcp_broker.rollout_simulator import _load_fleet_statuses

    fleet_path = tmp_path / "fleet.json"
    fleet_path.write_bytes(json.dumps({"label": "caf\u00e9"}, ensure_ascii=False).encode())

    assert _load_fleet_statuses(fleet_path) == [{"label": "caf\u00e9"}]


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"broker_id": "broker-a"}, "broker-a"),
        ({"broker_id": 7}, "unknown"),
        ({"broker_id": ""}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_rollout_simulator_broker_id_contract(
    identity: dict[str, object],
    expected: str,
) -> None:
    from mcp_broker.rollout_simulator import _broker_id

    assert _broker_id(identity) == expected


def test_rollout_simulator_range_allows_an_open_minimum() -> None:
    from mcp_broker.rollout_simulator import _in_range

    assert _in_range(2, None, 3) is True
