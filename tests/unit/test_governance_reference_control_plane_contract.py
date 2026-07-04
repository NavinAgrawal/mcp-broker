from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.bundles import minimal_bundle, write_signed_bundle
from mcp_broker.cli import main as cli_main


pytestmark = [pytest.mark.unit]

CREATED_AT = "2026-07-04T07:00:00Z"
APPROVAL_EXPIRES_AT = "2026-07-04T07:30:00Z"
PUBLISH_PROVENANCE = {
    "repository": "mcp-broker",
    "commit": "abc1234",
    "builder": "reference-control-plane",
}
SIGNATURE_REF = "sigstore:reference-control-plane.sig"


def test_reference_control_plane_exercises_local_governance_contracts(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_reference_control_plane import run_reference_control_plane

    bundle_path = _bundle_path(tmp_path)
    state_dir = tmp_path / "state"

    report = run_reference_control_plane(
        mode="local_reference_only",
        state_dir=state_dir,
        bundle_path=bundle_path,
        assignment_source=_assignment_source(),
        broker_context=_broker_context(),
        fleet_statuses=[_fleet_status("healthy")],
        target_url="https://control.example.invalid/fleet-status",
        auth_ref="env:GOVERNANCE_CONTROL_TOKEN",
        operator="release-operator",
        signature_ref=SIGNATURE_REF,
        provenance=PUBLISH_PROVENANCE,
        approval_expires_at=APPROVAL_EXPIRES_AT,
        created_at=CREATED_AT,
    )

    report_path = Path(str(report["report_path"]))
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
    approval_record = json.loads(Path(str(report["approval"]["record_path"])).read_text())

    assert report["schema_version"] == 1
    assert report["mode"] == "local_reference_only"
    assert report["changed_runtime_state"] is False
    assert report["contracts"] == [
        "publish",
        "assign",
        "collect",
        "rollout_control",
        "approve",
        "rollback",
    ]
    assert Path(str(report["publish"]["manifest_path"])).is_file()
    assert report["assignment"]["assignment_id"] == "platform-canary"
    assert report["collection"]["upload"]["attempted"] is False
    assert report["rollout_control"]["action_count"] == 1
    assert report["approval"]["request_type"] == "rollout"
    assert approval_record["target"] == {"action_ids": ["0001-broker-west-1-canary"]}
    assert report["rollback"]["state"] == "not_required"
    assert saved_report == report


def test_reference_control_plane_records_rollback_approval_for_unhealthy_fleet(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_reference_control_plane import run_reference_control_plane

    report = run_reference_control_plane(
        mode="local_reference_only",
        state_dir=tmp_path / "state",
        bundle_path=_bundle_path(tmp_path),
        assignment_source=_assignment_source(),
        broker_context=_broker_context(),
        fleet_statuses=[_fleet_status("degraded")],
        target_url="https://control.example.invalid/fleet-status",
        auth_ref="env:GOVERNANCE_CONTROL_TOKEN",
        operator="release-operator",
        signature_ref=SIGNATURE_REF,
        provenance=PUBLISH_PROVENANCE,
        approval_expires_at=APPROVAL_EXPIRES_AT,
        created_at=CREATED_AT,
    )

    rollback = report["rollback"]
    approval_record = json.loads(Path(str(rollback["approval_record_path"])).read_text())

    assert report["rollout_control"]["action_count"] == 1
    assert report["approval"]["request_type"] == "rollback"
    assert rollback["state"] == "approval_recorded"
    assert rollback["action_ids"] == ["0001-broker-west-1-rollback"]
    assert approval_record["request_type"] == "rollback"
    assert approval_record["changed_runtime_state"] is False


def test_reference_control_plane_rejects_non_local_mode_and_secret_upload_url(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_reference_control_plane import (
        GovernanceReferenceControlPlaneError,
        run_reference_control_plane,
    )

    common_args = {
        "state_dir": tmp_path / "state",
        "bundle_path": _bundle_path(tmp_path),
        "assignment_source": _assignment_source(),
        "broker_context": _broker_context(),
        "fleet_statuses": [_fleet_status("healthy")],
        "auth_ref": "env:GOVERNANCE_CONTROL_TOKEN",
        "operator": "release-operator",
        "signature_ref": SIGNATURE_REF,
        "provenance": PUBLISH_PROVENANCE,
        "approval_expires_at": APPROVAL_EXPIRES_AT,
        "created_at": CREATED_AT,
    }

    with pytest.raises(GovernanceReferenceControlPlaneError, match="local_reference_only"):
        run_reference_control_plane(
            mode="hosted_shared_runtime",
            target_url="https://control.example.invalid/fleet-status",
            **common_args,
        )
    with pytest.raises(GovernanceReferenceControlPlaneError, match="secret query data"):
        run_reference_control_plane(
            mode="local_reference_only",
            target_url="https://control.example.invalid/fleet-status?token=abc123",
            **common_args,
        )


def test_reference_control_plane_cli_emits_report_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli_main(
            [
                "governance",
                "reference-control-plane",
                "--state-dir",
                str(tmp_path / "state"),
                "--bundle",
                str(_bundle_path(tmp_path)),
                "--assignment-source",
                str(_write_json(tmp_path / "assignment.json", _assignment_source())),
                "--broker-context",
                str(_write_json(tmp_path / "context.json", _broker_context())),
                "--fleet-status",
                str(_write_json(tmp_path / "fleet-status.json", _fleet_status("healthy"))),
                "--target-url",
                "https://control.example.invalid/fleet-status",
                "--auth-ref",
                "env:GOVERNANCE_CONTROL_TOKEN",
                "--operator",
                "release-operator",
                "--signature-ref",
                SIGNATURE_REF,
                "--provenance",
                str(_write_json(tmp_path / "provenance.json", PUBLISH_PROVENANCE)),
                "--approval-expires-at",
                APPROVAL_EXPIRES_AT,
                "--created-at",
                CREATED_AT,
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert "governance reference control-plane report:" in stdout
    assert "reference-report.json" in stdout


def _bundle_path(tmp_path: Path) -> Path:
    return write_signed_bundle(tmp_path / "bundle.json", minimal_bundle())


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _assignment_source() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "platform-canary",
                "priority": 100,
                "match": {
                    "broker_ids": ["broker-west-1"],
                    "teams": ["platform"],
                    "channels": ["stable"],
                    "rings": ["canary"],
                },
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            }
        ],
    }


def _broker_context() -> dict[str, Any]:
    return {
        "broker_id": "broker-west-1",
        "user": "engineer-1",
        "teams": ["platform"],
        "channel": "stable",
        "ring": "canary",
    }


def _fleet_status(status: str) -> dict[str, Any]:
    return {
        "identity": {
            "active_profiles": ["codex"],
            "broker_id": "broker-west-1",
            "bundle_version": "2026.07.00",
            "environment": "local",
            "schema_version": 1,
        },
        "health": {
            "last_request_status": "ok",
            "started_at": CREATED_AT,
            "status": status,
            "updated_at": CREATED_AT,
        },
        "request_counters": {
            "request_errors_total": 0,
            "requests_total": 1,
        },
        "upstreams": {},
    }
