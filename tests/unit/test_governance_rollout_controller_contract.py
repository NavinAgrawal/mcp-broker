from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_broker.cli import main as cli_main


pytestmark = [pytest.mark.unit]


def test_rollout_controller_writes_auditable_actions_without_runtime_mutation(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_rollout_controller import control_rollout

    state_dir = tmp_path / "state"

    result = control_rollout(
        simulation=_ready_simulation(),
        state_dir=state_dir,
        operator="release-operator",
        bundle=_bundle_metadata(),
        created_at="2026-07-04T05:00:00Z",
    )

    assert result["schema_version"] == 1
    assert result["action_count"] == 3
    assert result["changed_runtime_state"] is False
    assert result["audit_log_path"] == str(
        state_dir / "governance-rollout" / "action-log.jsonl"
    )
    assert result["action_paths"] == [
        str(state_dir / "governance-rollout" / "actions" / "0001-broker-a-canary.json"),
        str(state_dir / "governance-rollout" / "actions" / "0002-broker-b-staged.json"),
        str(state_dir / "governance-rollout" / "actions" / "0003-broker-c-broad.json"),
    ]

    records = [
        json.loads(Path(action_path).read_text(encoding="utf-8"))
        for action_path in result["action_paths"]
    ]
    assert [record["action"] for record in records] == ["canary", "staged", "broad"]
    assert [record["broker_id"] for record in records] == [
        "broker-a",
        "broker-b",
        "broker-c",
    ]
    assert [record["stage"] for record in records] == ["canary", "staged", "broad"]
    assert [record["requires_approval"] for record in records] == [False, False, False]
    assert [record["changed_runtime_state"] for record in records] == [
        False,
        False,
        False,
    ]
    assert {record["operator"] for record in records} == {"release-operator"}
    assert {record["created_at"] for record in records} == {"2026-07-04T05:00:00Z"}
    assert {record["source_state"] for record in records} == {"ready"}
    assert {record["mode"] for record in records} == {"local_simulation_only"}
    assert {record["bundle"]["version"] for record in records} == {"2026.07.04"}
    assert {record["bundle"]["digest"]["value"] for record in records} == {"abc123"}

    audit_lines = (state_dir / "governance-rollout" / "action-log.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [json.loads(line)["action_id"] for line in audit_lines] == [
        "0001-broker-a-canary",
        "0002-broker-b-staged",
        "0003-broker-c-broad",
    ]


def test_rollout_controller_records_hold_when_approval_is_missing(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_rollout_controller import control_rollout

    result = control_rollout(
        simulation={
            "mode": "local_simulation_only",
            "state": "approval_required",
            "decisions": [],
            "reasons": ["policy approval_required is true and approval was not granted"],
        },
        state_dir=tmp_path / "state",
        operator="release-operator",
        bundle=_bundle_metadata(),
        created_at="2026-07-04T05:10:00Z",
    )

    record = json.loads(Path(result["action_paths"][0]).read_text(encoding="utf-8"))
    assert result["action_count"] == 1
    assert record["action"] == "hold"
    assert record["broker_id"] == "fleet"
    assert record["requires_approval"] is True
    assert record["reasons"] == [
        "policy approval_required is true and approval was not granted"
    ]
    assert record["changed_runtime_state"] is False


def test_rollout_controller_records_rollback_actions_for_unhealthy_brokers(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_rollout_controller import control_rollout

    result = control_rollout(
        simulation={
            "mode": "local_simulation_only",
            "state": "rollback",
            "decisions": [
                {"broker_id": "broker-a", "stage": "canary", "state": "rollback"}
            ],
            "reasons": ["broker-a health status degraded triggers rollback"],
        },
        state_dir=tmp_path / "state",
        operator="release-operator",
        bundle=_bundle_metadata(),
        created_at="2026-07-04T05:20:00Z",
    )

    record = json.loads(Path(result["action_paths"][0]).read_text(encoding="utf-8"))
    assert result["action_count"] == 1
    assert record["action"] == "rollback"
    assert record["broker_id"] == "broker-a"
    assert record["stage"] == "canary"
    assert record["requires_approval"] is False
    assert record["reasons"] == ["broker-a health status degraded triggers rollback"]


def test_rollout_controller_rejects_non_local_simulation(tmp_path: Path) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        control_rollout,
    )

    with pytest.raises(GovernanceRolloutControllerError, match="local simulation"):
        control_rollout(
            simulation={
                "mode": "remote_control_plane",
                "state": "ready",
                "decisions": [],
                "reasons": [],
            },
            state_dir=tmp_path / "state",
            operator="release-operator",
            bundle=_bundle_metadata(),
            created_at="2026-07-04T05:30:00Z",
        )


def test_rollout_controller_cli_writes_records_and_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    simulation_path = tmp_path / "simulation.json"
    simulation_path.write_text(json.dumps(_ready_simulation()), encoding="utf-8")

    assert (
        cli_main(
            [
                "governance",
                "rollout-control",
                "--simulation",
                str(simulation_path),
                "--state-dir",
                str(tmp_path / "state"),
                "--operator",
                "release-operator",
                "--bundle-id",
                "governance-bundle",
                "--bundle-version",
                "2026.07.04",
                "--bundle-channel",
                "stable",
                "--bundle-digest",
                "sha256:abc123",
                "--created-at",
                "2026-07-04T05:40:00Z",
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert "governance rollout actions recorded: 3" in stdout
    assert "record=" in stdout
    assert (tmp_path / "state" / "governance-rollout" / "action-log.jsonl").is_file()


def _ready_simulation() -> dict[str, object]:
    return {
        "mode": "local_simulation_only",
        "state": "ready",
        "decisions": [
            {"broker_id": "broker-a", "stage": "canary", "state": "canary"},
            {"broker_id": "broker-b", "stage": "staged", "state": "staged_rollout"},
            {"broker_id": "broker-c", "stage": "broad", "state": "broad_rollout"},
        ],
        "reasons": [],
    }


def _bundle_metadata() -> dict[str, object]:
    return {
        "bundle_id": "governance-bundle",
        "version": "2026.07.04",
        "channel": "stable",
        "digest": {
            "algorithm": "sha256",
            "value": "abc123",
        },
    }
