from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
from unittest.mock import Mock, mock_open

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


def test_rollout_controller_records_hold_for_compatibility_rejection(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_rollout_controller import control_rollout

    result = control_rollout(
        simulation={
            "mode": "local_simulation_only",
            "state": "compatibility_rejection",
            "decisions": [],
            "reasons": ["broker-a is not targeted"],
        },
        state_dir=tmp_path / "state",
        operator="release-operator",
        bundle=_bundle_metadata(),
        created_at="2026-07-04T05:11:00Z",
    )

    record = json.loads(Path(result["action_paths"][0]).read_text(encoding="utf-8"))
    assert record["action"] == "hold"
    assert record["requires_approval"] is False
    assert record["reasons"] == ["broker-a is not targeted"]


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


@pytest.mark.parametrize(
    ("simulation", "message"),
    [
        (
            {"mode": "local_simulation_only", "decisions": [], "reasons": []},
            "simulation state is required",
        ),
        (
            {
                "mode": "local_simulation_only",
                "state": "ready",
                "decisions": {},
                "reasons": [],
            },
            "simulation decisions must be a list",
        ),
        (
            {
                "mode": "local_simulation_only",
                "state": "ready",
                "decisions": [],
                "reasons": {},
            },
            "simulation reasons must be a list",
        ),
        (
            {
                "mode": "local_simulation_only",
                "state": "ready",
                "decisions": [],
                "reasons": [],
            },
            "simulation decisions are required",
        ),
        (
            {
                "mode": "local_simulation_only",
                "state": "ready",
                "decisions": [{"broker_id": "broker-a", "stage": "canary", "state": "paused"}],
                "reasons": [],
            },
            "unsupported decision state",
        ),
        (
            {
                "mode": "local_simulation_only",
                "state": "ready",
                "decisions": [{"stage": "canary", "state": "canary"}],
                "reasons": [],
            },
            "broker_id is required",
        ),
    ],
)
def test_rollout_controller_rejects_malformed_simulation(
    tmp_path: Path,
    simulation: dict[str, object],
    message: str,
) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        control_rollout,
    )

    with pytest.raises(GovernanceRolloutControllerError, match=message):
        control_rollout(
            simulation=simulation,
            state_dir=tmp_path / "state",
            operator="release-operator",
            bundle=_bundle_metadata(),
            created_at="2026-07-04T05:30:00Z",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bundle_id", "", "bundle_id is required"),
        ("version", "", "bundle_version is required"),
        ("channel", "", "bundle_channel is required"),
        ("digest.algorithm", "", "digest algorithm is required"),
        ("digest.value", "", "digest value is required"),
    ],
)
def test_rollout_controller_rejects_bad_bundle_metadata(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        control_rollout,
    )

    bundle = _bundle_metadata()
    if field.startswith("digest."):
        digest = bundle["digest"]
        assert isinstance(digest, dict)
        digest[field.split(".", maxsplit=1)[1]] = value
    else:
        bundle[field] = value

    with pytest.raises(GovernanceRolloutControllerError, match=message):
        control_rollout(
            simulation=_ready_simulation(),
            state_dir=tmp_path / "state",
            operator="release-operator",
            bundle=bundle,
            created_at="2026-07-04T05:30:00Z",
        )


def test_rollout_controller_rejects_empty_operator(tmp_path: Path) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        control_rollout,
    )

    with pytest.raises(GovernanceRolloutControllerError, match="operator is required"):
        control_rollout(
            simulation=_ready_simulation(),
            state_dir=tmp_path / "state",
            operator=" ",
            bundle=_bundle_metadata(),
            created_at="2026-07-04T05:30:00Z",
        )


def test_rollout_controller_rejects_empty_sanitized_action_id_component() -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        _safe_id_part,
    )

    with pytest.raises(GovernanceRolloutControllerError) as exc_info:
        _safe_id_part("!!!")
    assert str(exc_info.value) == "empty action id component"


def test_rollout_controller_safe_id_preserves_valid_x_characters() -> None:
    from mcp_broker.governance_rollout_controller import _safe_id_part

    assert _safe_id_part("XvalueX") == "XvalueX"


def test_rollout_controller_rejects_duplicate_action_records(tmp_path: Path) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        control_rollout,
    )

    kwargs = {
        "simulation": _ready_simulation(),
        "state_dir": tmp_path / "state",
        "operator": "release-operator",
        "bundle": _bundle_metadata(),
        "created_at": "2026-07-04T05:30:00Z",
    }
    control_rollout(**kwargs)

    with pytest.raises(GovernanceRolloutControllerError, match="rollout action already exists"):
        control_rollout(**kwargs)


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


def test_rollout_controller_direct_cli_reports_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_rollout_controller import main

    simulation_path = tmp_path / "simulation.json"
    simulation_path.write_text("[]", encoding="utf-8")

    assert (
        main(
            [
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
            ]
        )
        == 1
    )
    assert "expected JSON object" in capsys.readouterr().out


def test_rollout_controller_cli_rejects_bad_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_rollout_controller import main

    simulation_path = tmp_path / "simulation.json"
    simulation_path.write_text(json.dumps(_ready_simulation()), encoding="utf-8")

    assert (
        main(
            [
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
                "sha256",
            ]
        )
        == 1
    )
    assert "bundle digest must be algorithm:value" in capsys.readouterr().out


def test_rollout_controller_digest_parser_preserves_value_delimiters_and_trims() -> None:
    from mcp_broker.governance_rollout_controller import _parse_digest

    assert _parse_digest(" sha256 : abc:def ") == {
        "algorithm": "sha256",
        "value": "abc:def",
    }


@pytest.mark.parametrize(
    ("digest", "message"),
    [
        ("sha256", "bundle digest must be algorithm:value"),
        (":abc123", "digest algorithm is required"),
        ("sha256:", "digest value is required"),
    ],
)
def test_rollout_controller_digest_parser_rejects_incomplete_values(
    digest: str,
    message: str,
) -> None:
    from mcp_broker.governance_rollout_controller import (
        GovernanceRolloutControllerError,
        _parse_digest,
    )

    with pytest.raises(GovernanceRolloutControllerError) as exc_info:
        _parse_digest(digest)
    assert str(exc_info.value) == message


def test_rollout_controller_json_loader_opens_expanded_path_as_utf8() -> None:
    from mcp_broker.governance_rollout_controller import _load_json_mapping

    path = Mock()
    expanded_path = Mock()
    expanded_path.open = mock_open(read_data='{"state": "ready"}')
    path.expanduser.return_value = expanded_path

    assert _load_json_mapping(path) == {"state": "ready"}
    path.expanduser.assert_called_once_with()
    expanded_path.open.assert_called_once_with("r", encoding="utf-8")


def test_rollout_controller_parser_has_exact_public_contract() -> None:
    from mcp_broker.governance_rollout_controller import _parser

    parser = _parser()
    assert parser.description == "Record local rollout-control actions"
    actions = {action.dest: action for action in parser._actions}
    assert set(actions) == {
        "help",
        "simulation",
        "state_dir",
        "operator",
        "bundle_id",
        "bundle_version",
        "bundle_channel",
        "bundle_digest",
        "created_at",
    }
    for name in {
        "simulation",
        "state_dir",
        "operator",
        "bundle_id",
        "bundle_version",
        "bundle_channel",
        "bundle_digest",
    }:
        assert actions[name].required is True
    assert actions["created_at"].required is False
    assert actions["simulation"].type is Path
    assert actions["state_dir"].type is Path


@pytest.mark.error_simulation
def test_rollout_controller_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulation_path = tmp_path / "simulation.json"
    simulation_path.write_text(json.dumps(_ready_simulation()), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "governance_rollout_controller",
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
        ],
    )

    module_name = "mcp_broker.governance_rollout_controller"
    previous_module = sys.modules.pop(module_name, None)
    try:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module(module_name, run_name="__main__")
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert exit_info.value.code == 0


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
