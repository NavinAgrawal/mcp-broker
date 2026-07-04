from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.bundles import minimal_bundle, write_signed_bundle
from tests.support.repo_paths import make_command, repo_root


pytestmark = pytest.mark.journey

ROOT = repo_root()
AUTH_REF = "env:GOVERNANCE_FETCH_TOKEN"


def test_cli_governance_pull_apply_and_rollback_flow(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_bundle, first_decision = _assigned_bundle(tmp_path / "first", version="2026.07.01")
    second_bundle, second_decision = _assigned_bundle(tmp_path / "second", version="2026.07.02")
    first_decision_path = _write_json(tmp_path / "first-decision.json", first_decision)
    second_decision_path = _write_json(tmp_path / "second-decision.json", second_decision)
    first_approval_path = _write_json(tmp_path / "first-approval.json", _approval(first_decision))
    second_approval_path = _write_json(tmp_path / "second-approval.json", _approval(second_decision))

    first_pull = _run_cli(
        "governance",
        "pull",
        "--source",
        first_bundle.as_uri(),
        "--assignment-decision",
        str(first_decision_path),
        "--state-dir",
        str(state_dir),
        "--auth-ref",
        AUTH_REF,
        "--auth-present",
    )
    first_record = _record_path_from_stdout(first_pull.stdout)
    first_apply = _run_cli(
        "governance",
        "apply",
        "--pull-record",
        str(first_record),
        "--state-dir",
        str(state_dir),
        "--approval",
        str(first_approval_path),
    )

    second_pull = _run_cli(
        "governance",
        "pull",
        "--source",
        second_bundle.as_uri(),
        "--assignment-decision",
        str(second_decision_path),
        "--state-dir",
        str(state_dir),
        "--auth-ref",
        AUTH_REF,
        "--auth-present",
    )
    second_record = _record_path_from_stdout(second_pull.stdout)
    second_apply = _run_cli(
        "governance",
        "apply",
        "--pull-record",
        str(second_record),
        "--state-dir",
        str(state_dir),
        "--approval",
        str(second_approval_path),
    )
    rollback = _run_cli("governance", "rollback", "--state-dir", str(state_dir))

    assert "governance bundle pulled:" in first_pull.stdout
    assert "governance bundle applied:" in first_apply.stdout
    assert "governance bundle applied:" in second_apply.stdout
    assert "governance bundle rolled back:" in rollback.stdout
    active = json.loads((state_dir / "deployments" / "active.json").read_text(encoding="utf-8"))
    assert "2026.07.01" in active["deployment_id"]


def test_make_governance_pull_apply_requires_explicit_inputs(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    bundle_path, decision = _assigned_bundle(tmp_path)
    decision_path = _write_json(tmp_path / "decision.json", decision)
    approval_path = _write_json(tmp_path / "approval.json", _approval(decision))

    pull_result = subprocess.run(
        make_command(
            "governance-pull",
            f"GOVERNANCE_SOURCE={bundle_path.as_uri()}",
            f"GOVERNANCE_ASSIGNMENT_DECISION={decision_path}",
            f"GOVERNANCE_AUTH_REF={AUTH_REF}",
            "GOVERNANCE_AUTH_PRESENT=1",
            f"RUNTIME_ROOT={runtime_root}",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pull_record = _record_path_from_stdout(pull_result.stdout)
    apply_result = subprocess.run(
        make_command(
            "governance-apply",
            f"GOVERNANCE_PULL_RECORD={pull_record}",
            f"GOVERNANCE_APPROVAL={approval_path}",
            f"RUNTIME_ROOT={runtime_root}",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "governance bundle pulled:" in pull_result.stdout
    assert "governance bundle applied:" in apply_result.stdout
    assert (runtime_root / "state" / "deployments" / "active.json").is_file()


def test_make_governance_rollout_control_records_action_ledger(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    simulation_path = _write_json(
        tmp_path / "simulation.json",
        {
            "mode": "local_simulation_only",
            "state": "ready",
            "decisions": [
                {"broker_id": "broker-a", "stage": "canary", "state": "canary"}
            ],
            "reasons": [],
        },
    )

    result = subprocess.run(
        make_command(
            "governance-rollout-control",
            f"GOVERNANCE_ROLLOUT_SIMULATION={simulation_path}",
            "GOVERNANCE_ROLLOUT_OPERATOR=release-operator",
            "GOVERNANCE_ROLLOUT_BUNDLE_ID=governance-bundle",
            "GOVERNANCE_ROLLOUT_BUNDLE_VERSION=2026.07.04",
            "GOVERNANCE_ROLLOUT_BUNDLE_CHANNEL=stable",
            "GOVERNANCE_ROLLOUT_BUNDLE_DIGEST=sha256:abc123",
            "GOVERNANCE_ROLLOUT_CREATED_AT=2026-07-04T05:50:00Z",
            f"RUNTIME_ROOT={runtime_root}",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "governance rollout actions recorded: 1" in result.stdout
    assert (runtime_root / "state" / "governance-rollout" / "action-log.jsonl").is_file()


def test_make_governance_approve_records_expiring_approval(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    result = subprocess.run(
        make_command(
            "governance-approve",
            f"RUNTIME_ROOT={runtime_root}",
            "GOVERNANCE_APPROVAL_REQUEST_TYPE=rollout",
            "GOVERNANCE_APPROVAL_OPERATOR=release-operator",
            "GOVERNANCE_APPROVAL_REASON=approve staged rollout",
            "GOVERNANCE_APPROVAL_EXPIRES_AT=2026-07-04T06:30:00Z",
            "GOVERNANCE_APPROVAL_ACTION_IDS=0001-broker-a-canary",
            "GOVERNANCE_APPROVAL_CREATED_AT=2026-07-04T06:00:00Z",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "governance approval recorded:" in result.stdout
    assert (runtime_root / "state" / "governance-approvals" / "audit.jsonl").is_file()


def test_make_governance_approve_ignores_inherited_shell_argument_state(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"

    result = subprocess.run(
        make_command(
            "governance-approve",
            f"RUNTIME_ROOT={runtime_root}",
            "GOVERNANCE_APPROVAL_REQUEST_TYPE=rollout",
            "GOVERNANCE_APPROVAL_OPERATOR=release-operator",
            "GOVERNANCE_APPROVAL_REASON=approve staged rollout",
            "GOVERNANCE_APPROVAL_EXPIRES_AT=2026-07-04T06:30:00Z",
            "GOVERNANCE_APPROVAL_CREATED_AT=2026-07-04T06:00:00Z",
        ),
        cwd=ROOT,
        env={**os.environ, "ACTION_ARGS": "--action-id polluted-from-env"},
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode != 0
    assert "at least one action id is required" in result.stdout
    assert not (runtime_root / "state" / "governance-approvals" / "audit.jsonl").exists()


def test_make_governance_reference_control_plane_writes_report(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    bundle_path = _bundle_for_reference_control_plane(tmp_path)
    assignment_path = _write_json(tmp_path / "assignment.json", _reference_assignment())
    context_path = _write_json(tmp_path / "context.json", _reference_context())
    fleet_path = _write_json(tmp_path / "fleet.json", _reference_fleet_status())
    provenance_path = _write_json(
        tmp_path / "provenance.json",
        {
            "repository": "mcp-broker",
            "commit": "abc1234",
            "builder": "reference-control-plane",
        },
    )

    result = subprocess.run(
        make_command(
            "governance-reference-control-plane",
            f"RUNTIME_ROOT={runtime_root}",
            f"GOVERNANCE_REFERENCE_BUNDLE={bundle_path}",
            f"GOVERNANCE_REFERENCE_ASSIGNMENT_SOURCE={assignment_path}",
            f"GOVERNANCE_REFERENCE_BROKER_CONTEXT={context_path}",
            f"GOVERNANCE_REFERENCE_FLEET_STATUS={fleet_path}",
            "GOVERNANCE_REFERENCE_TARGET_URL=https://control.example.invalid/fleet-status",
            "GOVERNANCE_REFERENCE_AUTH_REF=env:GOVERNANCE_CONTROL_TOKEN",
            "GOVERNANCE_REFERENCE_OPERATOR=release-operator",
            "GOVERNANCE_REFERENCE_SIGNATURE_REF=sigstore:reference-control-plane.sig",
            f"GOVERNANCE_REFERENCE_PROVENANCE={provenance_path}",
            "GOVERNANCE_REFERENCE_APPROVAL_EXPIRES_AT=2026-07-04T07:30:00Z",
            "GOVERNANCE_REFERENCE_CREATED_AT=2026-07-04T07:00:00Z",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    report_path = (
        runtime_root
        / "state"
        / "governance-reference-control-plane"
        / "reports"
        / "reference-report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "governance reference control-plane report:" in result.stdout
    assert report["contracts"] == [
        "publish",
        "assign",
        "collect",
        "rollout_control",
        "approve",
        "rollback",
    ]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mcp_broker.cli", *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _assigned_bundle(
    tmp_path: Path,
    *,
    version: str = "2026.07.01",
) -> tuple[Path, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bundle = minimal_bundle()
    bundle["version"] = version
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)
    loaded = json.loads(bundle_path.read_text(encoding="utf-8"))
    decision = {
        "schema_version": 1,
        "assignment_id": f"assignment-{version}",
        "target": {
            "bundle_id": loaded["bundle_id"],
            "version": loaded["version"],
            "channel": loaded["channel"],
            "digest": loaded["checksum"],
        },
        "changed_runtime_state": False,
    }
    return bundle_path, decision


def _bundle_for_reference_control_plane(tmp_path: Path) -> Path:
    return write_signed_bundle(tmp_path / "reference-bundle.json", minimal_bundle())


def _reference_assignment() -> dict[str, object]:
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


def _reference_context() -> dict[str, object]:
    return {
        "broker_id": "broker-west-1",
        "channel": "stable",
        "ring": "canary",
        "teams": ["platform"],
        "user": "engineer-1",
    }


def _reference_fleet_status() -> dict[str, object]:
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
            "started_at": "2026-07-04T07:00:00Z",
            "status": "healthy",
            "updated_at": "2026-07-04T07:00:00Z",
        },
        "request_counters": {
            "request_errors_total": 0,
            "requests_total": 1,
        },
        "upstreams": {},
    }


def _approval(decision: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "approved": True,
        "approved_by": "release-manager",
        "reason": "approved governance bundle rollout",
        "assignment_id": decision["assignment_id"],
        "target": decision["target"],
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _record_path_from_stdout(stdout: str) -> Path:
    marker = "record="
    for line in stdout.splitlines():
        if marker in line:
            return Path(line.split(marker, maxsplit=1)[1].strip())
    raise AssertionError(stdout)
