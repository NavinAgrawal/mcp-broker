from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_broker.cli import main as cli_main


pytestmark = [pytest.mark.unit]

CREATED_AT = "2026-07-04T06:00:00Z"
EXPIRES_AT = "2026-07-04T06:30:00Z"
ACTION_IDS = ["0001-broker-a-canary", "0002-broker-b-staged"]
POLICY_PATHS = ["policy.rollout.approval", "policy.bootstrap.apply"]


def test_governance_approval_records_rollout_targets_without_mutation(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_approval import create_approval

    state_dir = tmp_path / "state"

    approval = create_approval(
        state_dir=state_dir,
        request_type="rollout",
        operator="release-operator",
        reason="approve staged rollout",
        expires_at=EXPIRES_AT,
        action_ids=ACTION_IDS,
        policy_paths=[],
        break_glass_record_id=None,
        created_at=CREATED_AT,
    )

    record_path = Path(str(approval["record_path"]))
    audit_path = state_dir / "governance-approvals" / "audit.jsonl"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert approval["schema_version"] == 1
    assert approval["request_type"] == "rollout"
    assert approval["approved"] is True
    assert approval["changed_runtime_state"] is False
    assert approval["requires_apply_step"] is True
    assert record["operator"] == "release-operator"
    assert record["reason"] == "approve staged rollout"
    assert record["expires_at"] == EXPIRES_AT
    assert record["target"] == {"action_ids": ACTION_IDS}
    assert record["changed_runtime_state"] is False
    assert audit_records == [
        {
            "approval_id": approval["approval_id"],
            "event": "governance_approval.created",
            "operator": "release-operator",
            "request_type": "rollout",
            "target": {"action_ids": ACTION_IDS},
            "ts": CREATED_AT,
        }
    ]


def test_governance_approval_records_policy_override_and_break_glass_targets(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_approval import create_approval

    policy_approval = create_approval(
        state_dir=tmp_path / "state",
        request_type="policy_override",
        operator="release-operator",
        reason="approve temporary policy override",
        expires_at=EXPIRES_AT,
        action_ids=[],
        policy_paths=POLICY_PATHS,
        break_glass_record_id=None,
        created_at=CREATED_AT,
    )
    break_glass_approval = create_approval(
        state_dir=tmp_path / "state",
        request_type="break_glass",
        operator="release-operator",
        reason="approve emergency break-glass record",
        expires_at=EXPIRES_AT,
        action_ids=[],
        policy_paths=[],
        break_glass_record_id="break-glass-abc123",
        created_at="2026-07-04T06:05:00Z",
    )

    policy_record = json.loads(Path(str(policy_approval["record_path"])).read_text(encoding="utf-8"))
    break_glass_record = json.loads(
        Path(str(break_glass_approval["record_path"])).read_text(encoding="utf-8")
    )
    assert policy_record["target"] == {"policy_paths": POLICY_PATHS}
    assert break_glass_record["target"] == {"break_glass_record_id": "break-glass-abc123"}


def test_governance_approval_rejects_missing_target_and_expired_approval(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_approval import GovernanceApprovalError, create_approval

    with pytest.raises(GovernanceApprovalError, match="at least one action id"):
        create_approval(
            state_dir=tmp_path / "state",
            request_type="rollback",
            operator="release-operator",
            reason="approve rollback",
            expires_at=EXPIRES_AT,
            action_ids=[],
            policy_paths=[],
            break_glass_record_id=None,
            created_at=CREATED_AT,
        )
    with pytest.raises(GovernanceApprovalError, match="expires_at must be in the future"):
        create_approval(
            state_dir=tmp_path / "state",
            request_type="rollout",
            operator="release-operator",
            reason="approve staged rollout",
            expires_at=CREATED_AT,
            action_ids=ACTION_IDS,
            policy_paths=[],
            break_glass_record_id=None,
            created_at=CREATED_AT,
        )


def test_governance_approval_cli_emits_record_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli_main(
            [
                "governance",
                "approve",
                "--state-dir",
                str(tmp_path / "state"),
                "--request-type",
                "rollout",
                "--operator",
                "release-operator",
                "--reason",
                "approve staged rollout",
                "--expires-at",
                EXPIRES_AT,
                "--action-id",
                ACTION_IDS[0],
                "--created-at",
                CREATED_AT,
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert "governance approval recorded:" in stdout
    assert "record=" in stdout
