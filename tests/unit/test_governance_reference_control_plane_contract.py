from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY

import pytest

from tests.support.bundles import minimal_bundle, write_signed_bundle
from mcp_broker.cli import main as cli_main
from mcp_broker import governance_reference_control_plane as control_plane


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


def test_reference_control_plane_rejects_empty_fleet_statuses(tmp_path: Path) -> None:
    from mcp_broker.governance_reference_control_plane import (
        GovernanceReferenceControlPlaneError,
        run_reference_control_plane,
    )

    with pytest.raises(GovernanceReferenceControlPlaneError, match="at least one fleet status"):
        run_reference_control_plane(
            mode="local_reference_only",
            state_dir=tmp_path / "state",
            bundle_path=_bundle_path(tmp_path),
            assignment_source=_assignment_source(),
            broker_context=_broker_context(),
            fleet_statuses=[],
            target_url="https://control.example.invalid/fleet-status",
            auth_ref="env:GOVERNANCE_CONTROL_TOKEN",
            operator="release-operator",
            signature_ref=SIGNATURE_REF,
            provenance=PUBLISH_PROVENANCE,
            approval_expires_at=APPROVAL_EXPIRES_AT,
            created_at=CREATED_AT,
        )


def test_reference_control_plane_uses_current_utc_when_created_at_is_omitted(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_reference_control_plane import run_reference_control_plane

    report = run_reference_control_plane(
        mode="local_reference_only",
        state_dir=tmp_path / "state",
        bundle_path=_bundle_path(tmp_path),
        assignment_source=_assignment_source(),
        broker_context=_broker_context(),
        fleet_statuses=[_fleet_status("healthy")],
        target_url="https://control.example.invalid/fleet-status",
        auth_ref="env:GOVERNANCE_CONTROL_TOKEN",
        operator="release-operator",
        signature_ref=SIGNATURE_REF,
        provenance=PUBLISH_PROVENANCE,
        approval_expires_at="2999-07-04T07:30:00Z",
    )

    assert str(report["created_at"]).endswith("Z")


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


def test_reference_control_plane_direct_cli_reports_invalid_json_objects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_reference_control_plane import main

    assert (
        main(
            [
                "--state-dir",
                str(tmp_path / "state"),
                "--bundle",
                str(_bundle_path(tmp_path)),
                "--assignment-source",
                str(_write_json(tmp_path / "assignment.json", [])),
                "--broker-context",
                str(_write_json(tmp_path / "context.json", _broker_context())),
                "--fleet-status",
                str(_write_json(tmp_path / "fleet-status.json", [_fleet_status("healthy")])),
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
        == 1
    )

    assert "expected JSON object" in capsys.readouterr().out


@pytest.mark.error_simulation
def test_reference_control_plane_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "governance_reference_control_plane",
            "--state-dir",
            str(tmp_path / "state"),
            "--bundle",
            str(_bundle_path(tmp_path)),
            "--assignment-source",
            str(_write_json(tmp_path / "assignment.json", _assignment_source())),
            "--broker-context",
            str(_write_json(tmp_path / "context.json", _broker_context())),
            "--fleet-status",
            str(_write_json(tmp_path / "fleet-status.json", [_fleet_status("healthy")])),
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
        ],
    )

    module_name = "mcp_broker.governance_reference_control_plane"
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


@pytest.mark.parametrize(
    ("broker_context", "expected_collector", "expected_simulation_broker", "expected_stage"),
    [
        ({}, "reference-control-plane", "reference-broker", "canary"),
        (
            {"broker_id": "broker-1", "ring": "preview"},
            "broker-1",
            "broker-1",
            "preview",
        ),
    ],
)
def test_reference_control_plane_calls_each_contract_with_exact_boundary_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broker_context: dict[str, Any],
    expected_collector: str,
    expected_simulation_broker: str,
    expected_stage: str,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    state_dir = tmp_path / "state"
    manifest_path = tmp_path / "published.json"
    report_path = state_dir / "governance-reference-control-plane" / "reports" / "reference-report.json"
    bundle = {"bundle_id": "bundle-a", "version": "1"}
    manifest = {"bundle": {"bundle_id": "bundle-a", "version": "1"}}
    assignment = {"target": {"bundle_id": "bundle-a", "version": "1"}}
    collection = {"upload": {"attempted": False}}
    simulation = {"stages": [{"name": "canary"}]}
    rollout = {"action_paths": [str(tmp_path / "action.json")]}
    approval = {"record_path": str(tmp_path / "approval.json")}
    calls: dict[str, Any] = {}
    load_results = iter([bundle, manifest])

    monkeypatch.setattr(control_plane, "_load_json_mapping", lambda path: next(load_results))

    def fake_publish_bundle(**kwargs: Any) -> Path:
        calls["publish"] = kwargs
        return manifest_path

    def fake_evaluate_assignment(**kwargs: Any) -> dict[str, Any]:
        calls["assign"] = kwargs
        return assignment

    def fake_prepare_collection(status: Any, **kwargs: Any) -> dict[str, Any]:
        calls["collect"] = (status, kwargs)
        return collection

    def fake_simulate_rollout(**kwargs: Any) -> dict[str, Any]:
        calls["simulate"] = kwargs
        return simulation

    def fake_control_rollout(**kwargs: Any) -> dict[str, Any]:
        calls["control"] = kwargs
        return rollout

    monkeypatch.setattr(control_plane, "publish_bundle", fake_publish_bundle)
    monkeypatch.setattr(control_plane, "evaluate_assignment", fake_evaluate_assignment)
    monkeypatch.setattr(control_plane, "prepare_collection_envelope", fake_prepare_collection)
    monkeypatch.setattr(control_plane, "simulate_rollout", fake_simulate_rollout)
    monkeypatch.setattr(control_plane, "control_rollout", fake_control_rollout)
    monkeypatch.setattr(control_plane, "_action_ids", lambda value: ["action-1"])
    monkeypatch.setattr(control_plane, "_has_rollback_action", lambda value: True)

    def fake_approval(**kwargs: Any) -> dict[str, Any]:
        calls["approval"] = kwargs
        return approval

    def fake_rollback(**kwargs: Any) -> dict[str, Any]:
        calls["rollback"] = kwargs
        return {"state": "approval_recorded"}

    def fake_write(path: Path, payload: Any) -> None:
        calls["write"] = (path, payload)

    monkeypatch.setattr(control_plane, "_create_rollout_or_rollback_approval", fake_approval)
    monkeypatch.setattr(control_plane, "_rollback_summary", fake_rollback)
    monkeypatch.setattr(control_plane, "_write_json_atomic", fake_write)

    fleet_statuses = [{"identity": {"broker_id": "broker-1"}}]
    report = control_plane.run_reference_control_plane(
        mode=control_plane.LOCAL_REFERENCE_MODE,
        state_dir=state_dir,
        bundle_path=bundle_path,
        assignment_source={"assignments": []},
        broker_context=broker_context,
        fleet_statuses=fleet_statuses,
        target_url="https://control.example.invalid/status",
        auth_ref="env:CONTROL_TOKEN",
        operator="operator-1",
        signature_ref="sigstore:bundle.sig",
        provenance={"commit": "abc123"},
        approval_expires_at=APPROVAL_EXPIRES_AT,
        created_at=CREATED_AT,
    )

    assert calls["publish"] == {
        "bundle_path": bundle_path,
        "output_dir": state_dir / "governance-reference-control-plane" / "published",
        "signature_ref": "sigstore:bundle.sig",
        "provenance": {"commit": "abc123"},
        "promotion_state": "candidate",
    }
    assert calls["assign"] == {
        "assignment_source": {"assignments": []},
        "published_manifests": [manifest],
        "broker_context": broker_context,
    }
    assert calls["collect"] == (
        fleet_statuses[0],
        {
            "target_url": "https://control.example.invalid/status",
            "auth_ref": "env:CONTROL_TOKEN",
            "retention_days": 30,
            "generated_at": CREATED_AT,
            "collector_id": expected_collector,
        },
    )
    assert calls["simulate"] == {
        "bundle": {
            "bundle_id": "bundle-a",
            "version": "1",
            "rollout": {
                "rollback_on_statuses": ["degraded"],
                "stages": [
                    {"name": expected_stage, "broker_ids": [expected_simulation_broker]}
                ],
            },
        },
        "fleet_statuses": fleet_statuses,
        "approval_granted": True,
    }
    assert calls["control"] == {
        "simulation": simulation,
        "state_dir": state_dir,
        "operator": "operator-1",
        "bundle": assignment["target"],
        "created_at": CREATED_AT,
    }
    assert calls["approval"] == {
        "state_dir": state_dir,
        "action_ids": ["action-1"],
        "rollback_required": True,
        "operator": "operator-1",
        "approval_expires_at": APPROVAL_EXPIRES_AT,
        "created_at": CREATED_AT,
    }
    assert calls["rollback"] == {
        "approval": approval,
        "action_ids": ["action-1"],
        "rollback_required": True,
    }
    assert report == {
        "schema_version": 1,
        "mode": "local_reference_only",
        "contracts": ["publish", "assign", "collect", "rollout_control", "approve", "rollback"],
        "created_at": CREATED_AT,
        "publish": {
            "manifest_path": str(manifest_path),
            "bundle": manifest["bundle"],
            "changed_runtime_state": False,
        },
        "assignment": assignment,
        "collection": collection,
        "rollout_simulation": simulation,
        "rollout_control": rollout,
        "approval": approval,
        "rollback": {"state": "approval_recorded"},
        "changed_runtime_state": False,
        "report_path": str(report_path),
    }
    assert calls["write"] == (report_path, report)


@pytest.mark.parametrize(
    ("rollback_required", "request_type", "reason"),
    [
        (False, "rollout", "reference control-plane rollout approval"),
        (True, "rollback", "reference control-plane rollback approval"),
    ],
)
def test_reference_approval_has_exact_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_required: bool,
    request_type: str,
    reason: str,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_approval(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"approval_id": "approval-1"}

    monkeypatch.setattr(control_plane, "create_approval", fake_create_approval)
    result = control_plane._create_rollout_or_rollback_approval(
        state_dir=tmp_path,
        action_ids=["a1", "a2"],
        rollback_required=rollback_required,
        operator="operator-1",
        approval_expires_at=APPROVAL_EXPIRES_AT,
        created_at=CREATED_AT,
    )

    assert result == {"approval_id": "approval-1"}
    assert captured == {
        "state_dir": tmp_path,
        "request_type": request_type,
        "operator": "operator-1",
        "reason": reason,
        "expires_at": APPROVAL_EXPIRES_AT,
        "action_ids": ["a1", "a2"],
        "policy_paths": [],
        "break_glass_record_id": None,
        "created_at": CREATED_AT,
    }


def test_reference_rollback_summary_has_exact_contract() -> None:
    approval = {"record_path": "/tmp/approval.json"}
    assert control_plane._rollback_summary(
        approval=approval, action_ids=["a1"], rollback_required=False
    ) == {"state": "not_required", "changed_runtime_state": False}
    assert control_plane._rollback_summary(
        approval=approval, action_ids=["a1"], rollback_required=True
    ) == {
        "state": "approval_recorded",
        "action_ids": ["a1"],
        "approval_record_path": "/tmp/approval.json",
        "changed_runtime_state": False,
    }


@pytest.mark.parametrize(
    ("context", "expected_broker_id", "expected_stage"),
    [
        ({}, "reference-broker", "canary"),
        ({"broker_id": 42, "ring": ""}, "42", "canary"),
        ({"broker_id": "broker-1", "ring": "preview"}, "broker-1", "preview"),
    ],
)
def test_reference_simulation_bundle_has_exact_contract(
    context: dict[str, Any], expected_broker_id: str, expected_stage: str
) -> None:
    source = {"bundle_id": "bundle-a", "rollout": {"old": True}}
    result = control_plane._simulation_bundle(bundle=source, broker_context=context)
    assert result == {
        "bundle_id": "bundle-a",
        "rollout": {
            "rollback_on_statuses": ["degraded"],
            "stages": [{"name": expected_stage, "broker_ids": [expected_broker_id]}],
        },
    }
    assert source == {"bundle_id": "bundle-a", "rollout": {"old": True}}


def test_reference_action_helpers_load_exact_action_contract(
    tmp_path: Path,
) -> None:
    rollout_path = _write_json(
        tmp_path / "rollout.json", {"action_id": 7, "action": "rollout"}
    )
    rollback_path = _write_json(
        tmp_path / "rollback.json", {"action_id": "a2", "action": "rollback"}
    )
    control = {"action_paths": [str(rollout_path), str(rollback_path)]}

    assert control_plane._has_rollback_action(control) is True
    assert control_plane._action_ids(control) == ["7", "a2"]
    assert control_plane._has_rollback_action({"action_paths": [str(rollout_path)]}) is False
    assert control_plane._has_rollback_action({}) is False
    assert control_plane._action_ids({}) == []


def test_reference_primary_fleet_status_contract() -> None:
    first = {"identity": {"broker_id": "first"}}
    second = {"identity": {"broker_id": "second"}}
    assert control_plane._primary_fleet_status([first, second]) is first
    with pytest.raises(control_plane.GovernanceReferenceControlPlaneError) as exc_info:
        control_plane._primary_fleet_status([])
    assert str(exc_info.value) == "at least one fleet status is required"


def test_reference_json_helpers_preserve_atomic_format_and_validation(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested" / "payload.json"
    payload = {"z": 1, "a": {"value": True}}
    control_plane._write_json_atomic(nested, payload)

    assert nested.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "value": true\n  },\n  "z": 1\n}\n'
    )
    assert not nested.with_name("payload.json.tmp").exists()
    assert control_plane._load_json_mapping(nested) == payload

    non_mapping = _write_json(tmp_path / "list.json", [1])
    with pytest.raises(
        control_plane.GovernanceReferenceControlPlaneError,
        match=f"expected JSON object: {non_mapping}",
    ):
        control_plane._load_json_mapping(non_mapping)


def test_reference_json_writer_uses_exact_atomic_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    class FakeParent:
        def mkdir(self, **kwargs: Any) -> None:
            events.append(("mkdir", kwargs))

    class FakeTemp:
        def write_text(self, value: str, **kwargs: Any) -> None:
            events.append(("write_text", value, kwargs))

    class FakePath:
        name = "report.json"
        parent = FakeParent()

        def with_name(self, name: str) -> FakeTemp:
            events.append(("with_name", name))
            return FakeTemp()

    monkeypatch.setattr(control_plane.os, "replace", lambda source, target: events.append(("replace", source, target)))
    path = FakePath()
    control_plane._write_json_atomic(path, {"b": 2, "a": 1})  # type: ignore[arg-type]

    assert events == [
        ("mkdir", {"parents": True, "exist_ok": True}),
        ("with_name", "report.json.tmp"),
        ("write_text", '{\n  "a": 1,\n  "b": 2\n}\n', {"encoding": "utf-8"}),
        ("replace", ANY, path),
    ]


def test_reference_fleet_loader_and_mapping_contract(tmp_path: Path) -> None:
    mapping_path = _write_json(tmp_path / "mapping.json", {"identity": {"broker_id": "b1"}})
    list_path = _write_json(tmp_path / "list.json", [{"a": 1}, "invalid", 3])

    assert control_plane._load_fleet_statuses(mapping_path) == [
        {"identity": {"broker_id": "b1"}}
    ]
    assert control_plane._load_fleet_statuses(list_path) == [{"a": 1}, {}, {}]
    mapping = {"a": 1}
    assert control_plane._mapping(mapping) is mapping
    assert control_plane._mapping(None) == {}


def test_reference_parser_contract() -> None:
    parser = control_plane._parser()
    assert parser.description == "Run the local reference control-plane flow"
    actions = {action.dest: action for action in parser._actions}
    assert set(actions) == {
        "help",
        "mode",
        "state_dir",
        "bundle",
        "assignment_source",
        "broker_context",
        "fleet_status",
        "target_url",
        "auth_ref",
        "operator",
        "signature_ref",
        "provenance",
        "approval_expires_at",
        "created_at",
    }
    assert actions["mode"].default == "local_reference_only"
    assert actions["mode"].required is False
    assert actions["created_at"].required is False
    for name in {
        "state_dir",
        "bundle",
        "assignment_source",
        "broker_context",
        "fleet_status",
        "target_url",
        "auth_ref",
        "operator",
        "signature_ref",
        "provenance",
        "approval_expires_at",
    }:
        assert actions[name].required is True
    for name in {"state_dir", "bundle", "assignment_source", "broker_context", "fleet_status", "provenance"}:
        assert actions[name].type is Path


def test_reference_main_forwards_exact_arguments_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assignment = {"assignments": []}
    context = {"broker_id": "b1"}
    fleet = [{"identity": {"broker_id": "b1"}}]
    provenance = {"commit": "abc"}
    args = SimpleNamespace(
        mode="local_reference_only",
        state_dir=tmp_path / "state",
        bundle=tmp_path / "bundle.json",
        assignment_source=tmp_path / "assignment.json",
        broker_context=tmp_path / "context.json",
        fleet_status=tmp_path / "fleet.json",
        target_url="https://control.example.invalid/status",
        auth_ref="env:TOKEN",
        operator="operator-1",
        signature_ref="sigstore:bundle.sig",
        provenance=tmp_path / "provenance.json",
        approval_expires_at=APPROVAL_EXPIRES_AT,
        created_at=CREATED_AT,
    )
    parser = SimpleNamespace(parse_args=lambda argv: args)
    monkeypatch.setattr(control_plane, "_parser", lambda: parser)
    loaded = {
        args.assignment_source: assignment,
        args.broker_context: context,
        args.provenance: provenance,
    }
    monkeypatch.setattr(control_plane, "_load_json_mapping", lambda path: loaded[path])
    monkeypatch.setattr(control_plane, "_load_fleet_statuses", lambda path: fleet)
    captured: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"report_path": "/tmp/reference-report.json"}

    monkeypatch.setattr(control_plane, "run_reference_control_plane", fake_run)
    argv = ["--state-dir", "ignored"]
    assert control_plane.main(argv) == 0
    assert captured == {
        "mode": args.mode,
        "state_dir": args.state_dir,
        "bundle_path": args.bundle,
        "assignment_source": assignment,
        "broker_context": context,
        "fleet_statuses": fleet,
        "target_url": args.target_url,
        "auth_ref": args.auth_ref,
        "operator": args.operator,
        "signature_ref": args.signature_ref,
        "provenance": provenance,
        "approval_expires_at": args.approval_expires_at,
        "created_at": args.created_at,
    }
    assert capsys.readouterr().out == (
        "governance reference control-plane report: /tmp/reference-report.json\n"
    )


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
