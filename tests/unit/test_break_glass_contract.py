from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


def test_break_glass_timestamp_parser_normalizes_z_and_offsets() -> None:
    from mcp_broker.break_glass import _parse_timestamp

    expected = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)
    assert _parse_timestamp("2026-07-04T06:00:00Z") == expected
    assert _parse_timestamp("2026-07-04T02:00:00-04:00") == expected
    assert _parse_timestamp("2026-07-04T06:00:00Z").tzinfo is UTC

REASON = "Emergency rollout bypass for runtime recovery"
OPERATOR = "operator@example.com"
CREATED_AT = "2026-07-01T12:00:00Z"
EXPIRES_AT = "2026-07-01T12:30:00Z"
AFTER_EXPIRATION = "2026-07-01T12:31:00Z"
CLI_EXPIRES_AT = "2099-07-01T12:30:00Z"
BYPASSED_POLICY_PATHS = [
    "policy.rollout.approval",
    "policy.bootstrap.apply",
]


def test_break_glass_create_writes_active_record_pointer_and_audit_journal(
    tmp_path: Path,
) -> None:
    from mcp_broker.break_glass import BreakGlassStore

    state_dir = tmp_path / "state"

    record = BreakGlassStore(state_dir).create(
        reason=REASON,
        operator=OPERATOR,
        expires_at=EXPIRES_AT,
        bypassed_policy_paths=BYPASSED_POLICY_PATHS,
        created_at=CREATED_AT,
    )

    record_path = state_dir / "break-glass" / "records" / f"{record['record_id']}.json"
    active_pointer = json.loads(
        (state_dir / "break-glass" / "active.json").read_text(encoding="utf-8")
    )
    audit_records = [
        json.loads(line)
        for line in (state_dir / "break-glass" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert record["status"] == "active"
    assert record["created_at"] == CREATED_AT
    assert record["expires_at"] == EXPIRES_AT
    assert record["reason"] == REASON
    assert record["operator"] == OPERATOR
    assert record["bypassed_policy_paths"] == BYPASSED_POLICY_PATHS
    assert record["audit_path"] == str(state_dir / "break-glass" / "audit.jsonl")
    assert record_path.is_file()
    assert active_pointer == {
        "record_id": record["record_id"],
        "record_path": str(record_path),
    }
    assert audit_records == [
        {
            "event": "break_glass.created",
            "record_id": record["record_id"],
            "operator": OPERATOR,
            "reason": REASON,
            "bypassed_policy_paths": BYPASSED_POLICY_PATHS,
            "expires_at": EXPIRES_AT,
            "ts": CREATED_AT,
        }
    ]


def test_break_glass_rejects_expired_or_incomplete_records(
    tmp_path: Path,
) -> None:
    from mcp_broker.break_glass import BreakGlassError, BreakGlassStore

    store = BreakGlassStore(tmp_path / "state")

    with pytest.raises(BreakGlassError, match="expires_at must be in the future"):
        store.create(
            reason=REASON,
            operator=OPERATOR,
            expires_at=CREATED_AT,
            bypassed_policy_paths=BYPASSED_POLICY_PATHS,
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="reason is required"):
        store.create(
            reason=" ",
            operator=OPERATOR,
            expires_at=EXPIRES_AT,
            bypassed_policy_paths=BYPASSED_POLICY_PATHS,
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="operator is required"):
        store.create(
            reason=REASON,
            operator=" ",
            expires_at=EXPIRES_AT,
            bypassed_policy_paths=BYPASSED_POLICY_PATHS,
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="at least one bypassed policy path"):
        store.create(
            reason=REASON,
            operator=OPERATOR,
            expires_at=EXPIRES_AT,
            bypassed_policy_paths=[],
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="invalid bypassed policy path"):
        store.create(
            reason=REASON,
            operator=OPERATOR,
            expires_at=EXPIRES_AT,
            bypassed_policy_paths=["policy/rollout"],
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="invalid timestamp"):
        store.create(
            reason=REASON,
            operator=OPERATOR,
            expires_at="not-a-date",
            bypassed_policy_paths=BYPASSED_POLICY_PATHS,
            created_at=CREATED_AT,
        )
    with pytest.raises(BreakGlassError, match="timestamp must include timezone"):
        store.create(
            reason=REASON,
            operator=OPERATOR,
            expires_at="2026-07-01T12:30:00",
            bypassed_policy_paths=BYPASSED_POLICY_PATHS,
            created_at=CREATED_AT,
        )


def test_break_glass_status_requires_active_unexpired_record(
    tmp_path: Path,
) -> None:
    from mcp_broker.break_glass import BreakGlassError, BreakGlassStore

    state_dir = tmp_path / "state"
    store = BreakGlassStore(state_dir)
    created = store.create(
        reason=REASON,
        operator=OPERATOR,
        expires_at=EXPIRES_AT,
        bypassed_policy_paths=BYPASSED_POLICY_PATHS,
        created_at=CREATED_AT,
    )

    active_status = store.status(now=CREATED_AT)

    assert active_status["degraded"] is True
    assert active_status["status"] == "active"
    assert active_status["active_record"]["record_id"] == created["record_id"]

    expired_status = store.status(now=AFTER_EXPIRATION)
    assert expired_status == {
        "active_record": None,
        "degraded": False,
        "status": "inactive",
    }
    with pytest.raises(BreakGlassError, match="break-glass record expired"):
        store.require_active_record(now=AFTER_EXPIRATION)


def test_break_glass_require_active_record_returns_unexpired_record(tmp_path: Path) -> None:
    from mcp_broker.break_glass import BreakGlassStore

    store = BreakGlassStore(tmp_path / "state")
    created = store.create(
        reason=REASON,
        operator=OPERATOR,
        expires_at=EXPIRES_AT,
        bypassed_policy_paths=BYPASSED_POLICY_PATHS,
        created_at=CREATED_AT,
    )

    active = store.require_active_record(now=CREATED_AT)

    assert active["record_id"] == created["record_id"]


def test_break_glass_requires_active_record_when_pointer_is_missing(tmp_path: Path) -> None:
    from mcp_broker.break_glass import BreakGlassError, BreakGlassStore

    with pytest.raises(BreakGlassError, match="not active"):
        BreakGlassStore(tmp_path / "state").require_active_record(now=CREATED_AT)


def test_break_glass_rejects_mismatched_active_pointer(tmp_path: Path) -> None:
    from mcp_broker.break_glass import BreakGlassError, BreakGlassStore

    state_dir = tmp_path / "state"
    store = BreakGlassStore(state_dir)
    record = store.create(
        reason=REASON,
        operator=OPERATOR,
        expires_at=EXPIRES_AT,
        bypassed_policy_paths=BYPASSED_POLICY_PATHS,
        created_at=CREATED_AT,
    )
    active_pointer = state_dir / "break-glass" / "active.json"
    active_pointer.write_text(
        json.dumps(
            {
                "record_id": "different-record",
                "record_path": str(state_dir / "break-glass" / "records" / f"{record['record_id']}.json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BreakGlassError, match="active pointer does not match record"):
        store.status(now=CREATED_AT)


def test_break_glass_rejects_non_object_active_pointer(tmp_path: Path) -> None:
    from mcp_broker.break_glass import BreakGlassError, BreakGlassStore

    state_dir = tmp_path / "state"
    active_pointer = state_dir / "break-glass" / "active.json"
    active_pointer.parent.mkdir(parents=True)
    active_pointer.write_text("[]", encoding="utf-8")

    with pytest.raises(BreakGlassError, match="expected JSON object"):
        BreakGlassStore(state_dir).status(now=CREATED_AT)


def test_break_glass_cli_create_and_status_emit_sorted_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli

    state_dir = tmp_path / "state"

    assert (
        cli.main(
            [
                "break-glass",
                "create",
                "--state-dir",
                str(state_dir),
                "--reason",
                REASON,
                "--operator",
                OPERATOR,
                "--expires-at",
                CLI_EXPIRES_AT,
                "--bypass-policy",
                BYPASSED_POLICY_PATHS[0],
                "--bypass-policy",
                BYPASSED_POLICY_PATHS[1],
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)

    assert (
        cli.main(
            [
                "break-glass",
                "status",
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)

    assert created["status"] == "active"
    assert created["created_at"].endswith("Z")
    assert status["degraded"] is True
    assert status["active_record"]["record_id"] == created["record_id"]


def test_break_glass_direct_cli_reports_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.break_glass import main

    assert (
        main(
            [
                "create",
                "--state-dir",
                str(tmp_path / "state"),
                "--reason",
                " ",
                "--operator",
                OPERATOR,
                "--expires-at",
                CLI_EXPIRES_AT,
                "--bypass-policy",
                BYPASSED_POLICY_PATHS[0],
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "reason is required" in captured.err
    assert captured.out == ""


@pytest.mark.error_simulation
def test_break_glass_main_reports_unknown_dispatch_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.break_glass as break_glass

    monkeypatch.setattr(
        break_glass,
        "_parse_args",
        lambda _argv: SimpleNamespace(
            break_glass_command="unknown",
            state_dir=tmp_path / "state",
        ),
    )

    assert break_glass.main([]) == 1
    assert "unknown break-glass command" in capsys.readouterr().err


@pytest.mark.error_simulation
def test_break_glass_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "break_glass",
            "status",
            "--state-dir",
            str(tmp_path / "state"),
        ],
    )

    module_name = "mcp_broker.break_glass"
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
