from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys

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


def test_governance_approval_defaults_created_at_when_not_supplied(tmp_path: Path) -> None:
    from mcp_broker.governance_approval import create_approval

    approval = create_approval(
        state_dir=tmp_path / "state",
        request_type="rollout",
        operator="release-operator",
        reason="approve staged rollout",
        expires_at="2099-07-04T06:30:00Z",
        action_ids=ACTION_IDS,
        policy_paths=[],
        break_glass_record_id=None,
        created_at=None,
    )

    record = json.loads(Path(str(approval["record_path"])).read_text(encoding="utf-8"))

    assert isinstance(record["created_at"], str)
    assert str(record["created_at"]).endswith("Z")


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


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"request_type": "unknown"}, "unsupported request_type"),
        ({"operator": " "}, "operator is required"),
        ({"reason": " "}, "reason is required"),
        ({"expires_at": "not-a-date"}, "invalid timestamp"),
        ({"expires_at": "2026-07-04T06:30:00"}, "timestamp must include timezone"),
        ({"action_ids": ["bad/id"]}, "invalid action id"),
        ({"request_type": "policy_override", "action_ids": [], "policy_paths": []}, "policy path"),
        (
            {
                "request_type": "policy_override",
                "action_ids": [],
                "policy_paths": ["policy/override"],
            },
            "invalid policy path",
        ),
        (
            {
                "request_type": "break_glass",
                "action_ids": [],
                "break_glass_record_id": None,
            },
            "break_glass_record_id is required",
        ),
        (
            {
                "request_type": "break_glass",
                "action_ids": [],
                "break_glass_record_id": "bad/id",
            },
            "invalid break_glass_record_id",
        ),
    ],
)
def test_governance_approval_rejects_invalid_fields(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    from mcp_broker.governance_approval import GovernanceApprovalError, create_approval

    args = {
        "state_dir": tmp_path / "state",
        "request_type": "rollout",
        "operator": "release-operator",
        "reason": "approve staged rollout",
        "expires_at": EXPIRES_AT,
        "action_ids": ACTION_IDS,
        "policy_paths": [],
        "break_glass_record_id": None,
        "created_at": CREATED_AT,
    }
    args.update(kwargs)

    with pytest.raises(GovernanceApprovalError, match=message):
        create_approval(**args)


def test_governance_approval_target_builder_rejects_unknown_request_type() -> None:
    from mcp_broker.governance_approval import GovernanceApprovalError, _target_for_request

    with pytest.raises(GovernanceApprovalError, match="unsupported request_type"):
        _target_for_request(
            request_type="unknown",
            action_ids=[],
            policy_paths=[],
            break_glass_record_id=None,
        )


def test_governance_approval_policy_paths_requires_at_least_one_path() -> None:
    from mcp_broker.governance_approval import GovernanceApprovalError, _policy_paths

    with pytest.raises(GovernanceApprovalError) as exc_info:
        _policy_paths([])
    assert str(exc_info.value) == "at least one policy path is required"


def test_governance_approval_rejects_duplicate_record(tmp_path: Path) -> None:
    from mcp_broker.governance_approval import GovernanceApprovalError, create_approval

    args = {
        "state_dir": tmp_path / "state",
        "request_type": "rollout",
        "operator": "release-operator",
        "reason": "approve staged rollout",
        "expires_at": EXPIRES_AT,
        "action_ids": ACTION_IDS,
        "policy_paths": [],
        "break_glass_record_id": None,
        "created_at": CREATED_AT,
    }

    create_approval(**args)
    with pytest.raises(GovernanceApprovalError, match="approval record already exists"):
        create_approval(**args)


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


def test_governance_approval_direct_cli_reports_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_approval import main

    assert (
        main(
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
                CREATED_AT,
                "--action-id",
                ACTION_IDS[0],
                "--created-at",
                CREATED_AT,
            ]
        )
        == 1
    )

    assert "expires_at must be in the future" in capsys.readouterr().out


def test_governance_approval_main_forwards_all_fields_and_emits_exact_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import governance_approval

    calls: list[dict[str, object]] = []
    record_path = tmp_path / "approval.json"

    def create_approval(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"approval_id": "approval-123", "record_path": record_path}

    monkeypatch.setattr(governance_approval, "create_approval", create_approval)

    state_dir = tmp_path / "state"
    assert governance_approval.main(
        [
            "--state-dir",
            str(state_dir),
            "--request-type",
            "policy_override",
            "--operator",
            "release-operator",
            "--reason",
            "approve policy override",
            "--expires-at",
            EXPIRES_AT,
            "--action-id",
            "action-a",
            "--policy-path",
            "policy.alpha",
            "--break-glass-record-id",
            "break-glass-123",
            "--created-at",
            CREATED_AT,
        ]
    ) == 0
    assert calls == [
        {
            "state_dir": state_dir,
            "request_type": "policy_override",
            "operator": "release-operator",
            "reason": "approve policy override",
            "expires_at": EXPIRES_AT,
            "action_ids": ["action-a"],
            "policy_paths": ["policy.alpha"],
            "break_glass_record_id": "break-glass-123",
            "created_at": CREATED_AT,
        }
    ]
    assert capsys.readouterr().out == (
        f"governance approval recorded: approval-123 record={record_path}\n"
    )


@pytest.mark.error_simulation
def test_governance_approval_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "governance_approval",
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
        ],
    )

    module_name = "mcp_broker.governance_approval"
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


def test_governance_approval_parser_has_exact_public_contract() -> None:
    from mcp_broker.governance_approval import _parser

    parser = _parser()
    assert parser.description == "Record local governance approval"
    actions = {action.dest: action for action in parser._actions}
    assert set(actions) == {
        "help",
        "state_dir",
        "request_type",
        "operator",
        "reason",
        "expires_at",
        "action_id",
        "policy_path",
        "break_glass_record_id",
        "created_at",
    }
    for name in {"state_dir", "request_type", "operator", "reason", "expires_at"}:
        assert actions[name].required is True
    for name in {"action_id", "policy_path", "break_glass_record_id", "created_at"}:
        assert actions[name].required is False
    assert actions["state_dir"].type is Path
    assert actions["action_id"].default == []
    assert actions["policy_path"].default == []
    assert actions["action_id"].const is None
    assert actions["policy_path"].const is None

    parsed = parser.parse_args(
        [
            "--state-dir",
            "state",
            "--request-type",
            "rollout",
            "--operator",
            "operator",
            "--reason",
            "reason",
            "--expires-at",
            EXPIRES_AT,
            "--action-id",
            "action-one",
            "--action-id",
            "action-two",
            "--policy-path",
            "policy/one",
            "--policy-path",
            "policy/two",
        ]
    )
    assert parsed.action_id == ["action-one", "action-two"]
    assert parsed.policy_path == ["policy/one", "policy/two"]
