from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from tests.support.bundles import write_signed_bundle


pytestmark = pytest.mark.unit


def test_deployment_store_records_active_and_previous_pointers(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    first_bundle = write_signed_bundle(tmp_path / "first.json")
    second_bundle = write_signed_bundle(
        tmp_path / "second.json",
        {
            **_minimal_bundle("team-local", "2026.07.02"),
            "upstreams": {
                "catalog-cache": {
                    "enabled": True,
                    "mode": "shared",
                    "transport": "stdio",
                    "command": "catalog-cache-server",
                    "profiles": ["codex"],
                },
            },
        },
    )

    store = DeploymentStore(state_dir)
    first = store.record_deployment(first_bundle)
    second = store.record_deployment(second_bundle)

    assert first["deployment_id"] != second["deployment_id"]
    assert _read_json(state_dir / "deployments" / "active.json") == {
        "deployment_id": second["deployment_id"],
        "record_path": str(state_dir / "deployments" / "records" / f"{second['deployment_id']}.json"),
    }
    assert _read_json(state_dir / "deployments" / "previous.json") == {
        "deployment_id": first["deployment_id"],
        "record_path": str(state_dir / "deployments" / "records" / f"{first['deployment_id']}.json"),
    }
    assert _read_json(Path(second["record_path"]))["status"] == "active"
    assert _journal_actions(state_dir) == ["activate", "activate"]


def test_deployment_store_rolls_back_to_previous_deployment(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    store = DeploymentStore(state_dir)
    first = store.record_deployment(write_signed_bundle(tmp_path / "first.json"))
    second = store.record_deployment(
        write_signed_bundle(tmp_path / "second.json", _minimal_bundle("team-local", "2026.07.02"))
    )

    rollback = store.rollback()

    assert rollback["active_deployment_id"] == first["deployment_id"]
    assert rollback["previous_deployment_id"] == second["deployment_id"]
    assert _read_json(state_dir / "deployments" / "active.json")["deployment_id"] == first["deployment_id"]
    assert _read_json(state_dir / "deployments" / "previous.json")["deployment_id"] == second["deployment_id"]
    assert _journal_actions(state_dir) == ["activate", "activate", "rollback"]


def test_deployment_store_rejects_rollback_without_active_pointer(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentError, DeploymentStore

    store = DeploymentStore(tmp_path / "runtime" / "state")

    with pytest.raises(DeploymentError, match="active deployment pointer"):
        store.rollback()


def test_deployment_store_rejects_rollback_without_previous_pointer(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentError, DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    store = DeploymentStore(state_dir)
    store.record_deployment(write_signed_bundle(tmp_path / "first.json"))

    with pytest.raises(DeploymentError, match="previous deployment pointer"):
        store.rollback()


def test_deployment_store_rejects_rollback_when_previous_record_is_missing(
    tmp_path: Path,
) -> None:
    from mcp_broker.deployments import DeploymentError, DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    store = DeploymentStore(state_dir)
    first = store.record_deployment(write_signed_bundle(tmp_path / "first.json"))
    store.record_deployment(
        write_signed_bundle(tmp_path / "second.json", _minimal_bundle("team-local", "2026.07.02"))
    )
    Path(str(first["record_path"])).unlink()

    with pytest.raises(DeploymentError, match="deployment record not found"):
        store.rollback()


def test_deployment_store_recovers_from_partial_pointer_write(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    store = DeploymentStore(state_dir)
    first = store.record_deployment(write_signed_bundle(tmp_path / "first.json"))
    deployments_dir = state_dir / "deployments"
    (deployments_dir / "active.json").write_text(
        json.dumps(
            {
                "deployment_id": "missing-deployment",
                "record_path": str(deployments_dir / "records" / "missing-deployment.json"),
            }
        ),
        encoding="utf-8",
    )
    partial = deployments_dir / "active.json.tmp"
    partial.write_text("partial", encoding="utf-8")

    recovery = store.recover()

    assert recovery == {
        "active_deployment_id": first["deployment_id"],
        "recovered": True,
        "removed_partial_files": [str(partial)],
    }
    assert not partial.exists()
    assert _read_json(deployments_dir / "active.json")["deployment_id"] == first["deployment_id"]
    assert _journal_actions(state_dir) == ["activate", "recover"]


def test_deployment_store_recover_is_noop_without_deployments_dir(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentStore

    recovery = DeploymentStore(tmp_path / "runtime" / "state").recover()

    assert recovery == {
        "active_deployment_id": None,
        "recovered": False,
        "removed_partial_files": [],
    }


def test_deployment_store_recover_fails_when_records_dir_is_empty(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentError, DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    deployments_dir = state_dir / "deployments"
    (deployments_dir / "records").mkdir(parents=True)
    (deployments_dir / "active.json").write_text(
        json.dumps(
            {
                "deployment_id": "missing-deployment",
                "record_path": str(deployments_dir / "records" / "missing-deployment.json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentError, match="active deployment record"):
        DeploymentStore(state_dir).recover()


def test_deployment_store_recover_fails_when_active_record_missing_and_no_records_exist(
    tmp_path: Path,
) -> None:
    from mcp_broker.deployments import DeploymentError, DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    deployments_dir = state_dir / "deployments"
    deployments_dir.mkdir(parents=True)
    (deployments_dir / "active.json").write_text(
        json.dumps(
            {
                "deployment_id": "missing-deployment",
                "record_path": str(deployments_dir / "records" / "missing-deployment.json"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentError, match="active deployment record"):
        DeploymentStore(state_dir).recover()


def test_deployment_store_recover_promotes_latest_record_when_active_pointer_missing(
    tmp_path: Path,
) -> None:
    from mcp_broker.deployments import DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    store = DeploymentStore(state_dir)
    first = store.record_deployment(write_signed_bundle(tmp_path / "first.json"))
    (state_dir / "deployments" / "active.json").unlink()

    recovery = store.recover()

    assert recovery == {
        "active_deployment_id": first["deployment_id"],
        "recovered": True,
        "removed_partial_files": [],
    }
    assert _read_json(state_dir / "deployments" / "active.json")["deployment_id"] == first[
        "deployment_id"
    ]


def test_deployment_store_dry_run_stage_does_not_write_state(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentStore

    state_dir = tmp_path / "runtime" / "state"
    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    report = DeploymentStore(state_dir).dry_run_stage(bundle_path)

    assert report["bundle_path"] == str(bundle_path)
    assert report["would_change_runtime_state"] is False
    assert not (state_dir / "deployments").exists()


def test_deployments_cli_reports_stage_dry_run_and_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.deployments import main

    state_dir = tmp_path / "runtime" / "state"
    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    assert main(["stage", "--bundle", str(bundle_path), "--state-dir", str(state_dir), "--dry-run"]) == 0
    assert "deployment dry-run:" in capsys.readouterr().out

    assert main(["stage", "--bundle", str(bundle_path), "--state-dir", str(state_dir)]) == 0
    assert "deployment staged:" in capsys.readouterr().out


def test_deployments_cli_reports_rollback_and_recover(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.deployments import main

    state_dir = tmp_path / "runtime" / "state"
    first_bundle = write_signed_bundle(tmp_path / "first.json")
    second_bundle = write_signed_bundle(
        tmp_path / "second.json",
        _minimal_bundle("team-local", "2026.07.02"),
    )

    assert main(["stage", "--bundle", str(first_bundle), "--state-dir", str(state_dir)]) == 0
    capsys.readouterr()
    assert main(["stage", "--bundle", str(second_bundle), "--state-dir", str(state_dir)]) == 0
    capsys.readouterr()

    assert main(["rollback", "--state-dir", str(state_dir)]) == 0
    assert "deployment rolled back:" in capsys.readouterr().out

    assert main(["recover", "--state-dir", str(state_dir)]) == 0
    assert "deployment recovery:" in capsys.readouterr().out


def test_deployments_cli_reports_bundle_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.deployments import main

    bundle_path = tmp_path / "bad.json"
    bundle_path.write_text("[]", encoding="utf-8")

    assert main(["stage", "--bundle", str(bundle_path), "--state-dir", str(tmp_path / "state")]) == 1
    assert "bundle file must contain a JSON object" in capsys.readouterr().out


def test_deployment_helpers_reject_non_object_pointer_json(tmp_path: Path) -> None:
    from mcp_broker.deployments import DeploymentError, _read_json

    pointer_path = tmp_path / "active.json"
    pointer_path.write_text("[]", encoding="utf-8")

    with pytest.raises(DeploymentError, match="expected JSON object"):
        _read_json(pointer_path)


@pytest.mark.error_simulation
def test_deployments_main_reports_unknown_dispatch_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.deployments as deployments

    monkeypatch.setattr(
        deployments,
        "_parse_args",
        lambda _argv: SimpleNamespace(
            deployment_command="unknown",
            state_dir=tmp_path / "state",
        ),
    )

    with pytest.raises(deployments.DeploymentError, match="unknown deployment command"):
        deployments.main([])


@pytest.mark.error_simulation
def test_deployments_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = write_signed_bundle(tmp_path / "bundle.json")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deployments",
            "stage",
            "--bundle",
            str(bundle_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--dry-run",
        ],
    )

    module_name = "mcp_broker.deployments"
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


def _minimal_bundle(bundle_id: str, version: str) -> dict[str, object]:
    from tests.support.bundles import minimal_bundle

    bundle = minimal_bundle()
    bundle["bundle_id"] = bundle_id
    bundle["version"] = version
    return bundle


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _journal_actions(state_dir: Path) -> list[str]:
    journal = state_dir / "deployments" / "rollback-journal.jsonl"
    return [json.loads(line)["action"] for line in journal.read_text(encoding="utf-8").splitlines()]
