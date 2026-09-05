from __future__ import annotations

import argparse
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.mark.error_simulation
def test_cli_governance_dispatches_pull_apply_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_governance

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_governance, "governance_pull_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="pull",
                state_dir=tmp_path / "state",
                source="file:///bundle.json",
                assignment_decision=tmp_path / "assignment.json",
                auth_ref="env:GOVERNANCE_FETCH_TOKEN",
                auth_present=True,
            )
        )
        == 0
    )
    assert calls[-1] == [
        "pull",
        "--state-dir",
        str(tmp_path / "state"),
        "--source",
        "file:///bundle.json",
        "--assignment-decision",
        str(tmp_path / "assignment.json"),
        "--auth-ref",
        "env:GOVERNANCE_FETCH_TOKEN",
        "--auth-present",
    ]

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="apply",
                state_dir=tmp_path / "state",
                pull_record=tmp_path / "pull-record.json",
                approval=tmp_path / "approval.json",
            )
        )
        == 0
    )
    assert calls[-1] == [
        "apply",
        "--state-dir",
        str(tmp_path / "state"),
        "--pull-record",
        str(tmp_path / "pull-record.json"),
        "--approval",
        str(tmp_path / "approval.json"),
    ]

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(governance_command="rollback", state_dir=tmp_path / "state")
        )
        == 0
    )
    assert calls[-1] == ["rollback", "--state-dir", str(tmp_path / "state")]


@pytest.mark.error_simulation
def test_cli_governance_rollout_control_passes_optional_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_governance

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_governance, "rollout_controller_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="rollout-control",
                simulation=tmp_path / "simulation.json",
                state_dir=tmp_path / "state",
                operator="release-operator",
                bundle_id="governance-bundle",
                bundle_version="2026.07.05",
                bundle_channel="stable",
                bundle_digest="sha256:abc123",
                created_at="2026-07-05T10:00:00Z",
            )
        )
        == 0
    )

    assert calls == [
        [
            "--simulation",
            str(tmp_path / "simulation.json"),
            "--state-dir",
            str(tmp_path / "state"),
            "--operator",
            "release-operator",
            "--bundle-id",
            "governance-bundle",
            "--bundle-version",
            "2026.07.05",
            "--bundle-channel",
            "stable",
            "--bundle-digest",
            "sha256:abc123",
            "--created-at",
            "2026-07-05T10:00:00Z",
        ]
    ]


@pytest.mark.error_simulation
def test_cli_governance_approval_passes_optional_break_glass_and_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_governance

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_governance, "governance_approval_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="approve",
                state_dir=tmp_path / "state",
                request_type="rollout",
                operator="release-operator",
                reason="approve staged rollout",
                expires_at="2026-07-05T11:00:00Z",
                action_id=["action-a", "action-b"],
                policy_path=["policy.rollout"],
                break_glass_record_id="break-glass-1",
                created_at="2026-07-05T10:00:00Z",
            )
        )
        == 0
    )

    assert calls == [
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--request-type",
            "rollout",
            "--operator",
            "release-operator",
            "--reason",
            "approve staged rollout",
            "--expires-at",
            "2026-07-05T11:00:00Z",
            "--action-id",
            "action-a",
            "--action-id",
            "action-b",
            "--policy-path",
            "policy.rollout",
            "--break-glass-record-id",
            "break-glass-1",
            "--created-at",
            "2026-07-05T10:00:00Z",
        ]
    ]


@pytest.mark.error_simulation
def test_cli_governance_reference_control_plane_passes_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_governance

    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli_governance,
        "reference_control_plane_main",
        lambda argv: calls.append(argv) or 0,
    )

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="reference-control-plane",
                mode="local_reference",
                state_dir=tmp_path / "state",
                bundle=tmp_path / "bundle.json",
                assignment_source=tmp_path / "assignment.json",
                broker_context=tmp_path / "broker.json",
                fleet_status=tmp_path / "fleet.json",
                target_url="file:///bundle.json",
                auth_ref="env:GOVERNANCE_FETCH_TOKEN",
                operator="release-operator",
                signature_ref="sigstore:bundle.sig",
                provenance=tmp_path / "provenance.json",
                approval_expires_at="2026-07-05T11:00:00Z",
                created_at="2026-07-05T10:00:00Z",
            )
        )
        == 0
    )

    assert "--created-at" in calls[0]
    assert calls[0][calls[0].index("--created-at") + 1] == "2026-07-05T10:00:00Z"
    assert calls[0][:4] == ["--mode", "local_reference", "--state-dir", str(tmp_path / "state")]


def test_cli_governance_parser_wires_all_subcommands(tmp_path: Path) -> None:
    from mcp_broker import cli_governance

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cli_governance.add_governance_parser(subparsers)

    parsed = parser.parse_args(
        [
            "governance",
            "apply",
            "--pull-record",
            str(tmp_path / "pull.json"),
            "--state-dir",
            str(tmp_path / "state"),
            "--approval",
            str(tmp_path / "approval.json"),
        ]
    )

    assert parsed.command == "governance"
    assert parsed.governance_command == "apply"
    assert parsed.handler is cli_governance.handle_governance


@pytest.mark.error_simulation
def test_cli_governance_omits_absent_optional_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_governance

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_governance, "governance_pull_main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli_governance, "rollout_controller_main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli_governance, "governance_approval_main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(cli_governance, "reference_control_plane_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="pull",
                state_dir=tmp_path / "state",
                source="file:///bundle.json",
                assignment_decision=tmp_path / "assignment.json",
                auth_ref="env:GOVERNANCE_FETCH_TOKEN",
                auth_present=False,
            )
        )
        == 0
    )
    assert "--auth-present" not in calls[-1]

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="rollout-control",
                simulation=tmp_path / "simulation.json",
                state_dir=tmp_path / "state",
                operator="release-operator",
                bundle_id="governance-bundle",
                bundle_version="2026.07.05",
                bundle_channel="stable",
                bundle_digest="sha256:abc123",
                created_at=None,
            )
        )
        == 0
    )
    assert "--created-at" not in calls[-1]

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="approve",
                state_dir=tmp_path / "state",
                request_type="rollout",
                operator="release-operator",
                reason="approve staged rollout",
                expires_at="2026-07-05T11:00:00Z",
                action_id=[],
                policy_path=[],
                break_glass_record_id=None,
                created_at=None,
            )
        )
        == 0
    )
    assert "--action-id" not in calls[-1]
    assert "--policy-path" not in calls[-1]
    assert "--break-glass-record-id" not in calls[-1]
    assert "--created-at" not in calls[-1]

    assert (
        cli_governance.handle_governance(
            argparse.Namespace(
                governance_command="reference-control-plane",
                mode="local_reference",
                state_dir=tmp_path / "state",
                bundle=tmp_path / "bundle.json",
                assignment_source=tmp_path / "assignment.json",
                broker_context=tmp_path / "broker.json",
                fleet_status=tmp_path / "fleet.json",
                target_url="file:///bundle.json",
                auth_ref="env:GOVERNANCE_FETCH_TOKEN",
                operator="release-operator",
                signature_ref="sigstore:bundle.sig",
                provenance=tmp_path / "provenance.json",
                approval_expires_at="2026-07-05T11:00:00Z",
                created_at=None,
            )
        )
        == 0
    )
    assert "--created-at" not in calls[-1]
