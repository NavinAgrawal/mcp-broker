import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.bundles import minimal_bundle, write_signed_bundle


pytestmark = pytest.mark.unit


AUTH_REF = "env:GOVERNANCE_FETCH_TOKEN"
PUBLISH_PROVENANCE = {
    "repository": "mcp-broker",
    "commit": "abc1234",
    "builder": "local-publisher",
}
SIGNATURE_REF = "sigstore:governance-bundle.sig"


def test_pull_fetches_assigned_file_bundle_into_cache_without_applying(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_pull import pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)
    state_dir = tmp_path / "state"

    report = pull_assigned_bundle(
        source_url=bundle_path.as_uri(),
        assignment_decision=decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )

    cache_record_path = Path(str(report["cache_record_path"]))
    cached_bundle_path = Path(str(report["cached_bundle_path"]))
    cache_record = json.loads(cache_record_path.read_text(encoding="utf-8"))

    assert report["schema_version"] == 1
    assert report["action"] == "pull"
    assert report["assignment_id"] == decision["assignment_id"]
    assert report["target"] == decision["target"]
    assert report["auth"] == {
        "required": True,
        "auth_ref": AUTH_REF,
        "secret_stored": False,
    }
    assert report["changed_runtime_state"] is False
    assert cache_record["target"] == decision["target"]
    assert cache_record["changed_runtime_state"] is False
    assert cached_bundle_path.is_file()
    assert str(cached_bundle_path).startswith(str(state_dir / "governance-pull" / "cache"))
    assert not (state_dir / "deployments").exists()
    assert "governance-fetch-token" not in cache_record_path.read_text(encoding="utf-8").lower()


def test_pull_rejects_missing_auth_before_fetch(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)

    with pytest.raises(GovernancePullError, match="governance fetch auth is required"):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=False,
        )


@pytest.mark.parametrize("auth_ref", ["", "plain:GOVERNANCE_FETCH_TOKEN"])
def test_pull_rejects_invalid_auth_ref(tmp_path: Path, auth_ref: str) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)

    with pytest.raises(GovernancePullError, match="auth"):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=auth_ref,
            auth_present=True,
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda decision: decision.update({"schema_version": 99}), "schema_version"),
        (lambda decision: decision.update({"assignment_id": ""}), "assignment_id"),
        (lambda decision: decision.update({"target": None}), "assignment target"),
        (lambda decision: decision["target"].update({"digest": None}), "target digest"),
    ],
)
def test_pull_rejects_invalid_assignment_decision(
    tmp_path: Path,
    mutation: Any,
    match: str,
) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)
    mutation(decision)

    with pytest.raises(GovernancePullError, match=match):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


def test_pull_rejects_unsafe_cache_path_fields() -> None:
    from mcp_broker.governance_pull import GovernancePullError, _safe_cache_part

    with pytest.raises(GovernancePullError, match="unsafe governance cache field"):
        _safe_cache_part("team/local")


@pytest.mark.parametrize("source_url", ["ftp://localhost/bundle.json", "https://example.com/bundle.json"])
def test_pull_rejects_unsupported_or_non_localhost_sources(
    tmp_path: Path,
    source_url: str,
) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    _bundle_path, decision = _assigned_bundle(tmp_path)

    with pytest.raises(GovernancePullError, match="file:// or localhost"):
        pull_assigned_bundle(
            source_url=source_url,
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


def test_pull_rejects_missing_file_source(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    _bundle_path, decision = _assigned_bundle(tmp_path)

    with pytest.raises(GovernancePullError, match="source not found"):
        pull_assigned_bundle(
            source_url=(tmp_path / "missing.json").as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


@pytest.mark.error_simulation
def test_pull_fetches_localhost_source_to_temp_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import governance_pull

    bundle_path, decision = _assigned_bundle(tmp_path)
    state_dir = tmp_path / "state"

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return bundle_path.read_bytes()

    monkeypatch.setattr(governance_pull, "urlopen", lambda *_args, **_kwargs: Response())

    report = governance_pull.pull_assigned_bundle(
        source_url="http://localhost:9000/governance-bundle.json",
        assignment_decision=decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )

    assert report["action"] == "pull"
    assert not (state_dir / "governance-pull" / "tmp" / "fetched-bundle.json.tmp").exists()


@pytest.mark.error_simulation
def test_pull_reports_localhost_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import governance_pull

    _bundle_path, decision = _assigned_bundle(tmp_path)

    def raise_os_error(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(governance_pull, "urlopen", raise_os_error)

    with pytest.raises(governance_pull.GovernancePullError, match="governance fetch failed"):
        governance_pull.pull_assigned_bundle(
            source_url="http://127.0.0.1:9000/governance-bundle.json",
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


def test_pull_rejects_bundle_digest_mismatch(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)
    decision["target"]["digest"]["value"] = "f" * 64

    with pytest.raises(GovernancePullError, match="assigned bundle digest mismatch"):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("bundle_id", "other-bundle", "target mismatch"),
        ("version", "2099.01.01", "target mismatch"),
        ("algorithm", "sha512", "digest mismatch"),
    ],
)
def test_pull_rejects_target_metadata_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
    match: str,
) -> None:
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle_path, decision = _assigned_bundle(tmp_path)
    if field == "algorithm":
        decision["target"]["digest"]["algorithm"] = value
    else:
        decision["target"][field] = value

    with pytest.raises(GovernancePullError, match=match):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )


def test_pull_rejects_incompatible_bundle_before_cache(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import bundle_checksum
    from mcp_broker.governance_pull import GovernancePullError, pull_assigned_bundle

    bundle = minimal_bundle()
    bundle["compatibility"]["min_config_schema_version"] = 2
    bundle["compatibility"]["max_config_schema_version"] = 2
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)
    loaded = json.loads(bundle_path.read_text(encoding="utf-8"))
    decision = _assignment_decision(
        bundle_id=str(loaded["bundle_id"]),
        version=str(loaded["version"]),
        channel=str(loaded["channel"]),
        digest_value=bundle_checksum(loaded),
    )

    with pytest.raises(GovernancePullError, match="incompatible config schema version"):
        pull_assigned_bundle(
            source_url=bundle_path.as_uri(),
            assignment_decision=decision,
            state_dir=tmp_path / "state",
            auth_ref=AUTH_REF,
            auth_present=True,
        )
    assert not (tmp_path / "state" / "governance-pull").exists()


def test_apply_requires_local_approval_before_mutating_deployment_state(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_pull import (
        GovernancePullError,
        apply_cached_bundle,
        pull_assigned_bundle,
    )

    bundle_path, decision = _assigned_bundle(tmp_path)
    state_dir = tmp_path / "state"
    pull_report = pull_assigned_bundle(
        source_url=bundle_path.as_uri(),
        assignment_decision=decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )

    with pytest.raises(GovernancePullError, match="local approval is required"):
        apply_cached_bundle(
            pull_record_path=Path(str(pull_report["cache_record_path"])),
            state_dir=state_dir,
            approval_record={"schema_version": 1, "approved": False},
        )

    apply_report = apply_cached_bundle(
        pull_record_path=Path(str(pull_report["cache_record_path"])),
        state_dir=state_dir,
        approval_record=_approval(decision),
    )

    assert apply_report["schema_version"] == 1
    assert apply_report["action"] == "apply"
    assert apply_report["bundle_id"] == decision["target"]["bundle_id"]
    assert apply_report["bundle_version"] == decision["target"]["version"]
    assert apply_report["changed_runtime_state"] is True
    assert (state_dir / "deployments" / "active.json").is_file()


@pytest.mark.parametrize(
    ("approval_update", "match"),
    [
        ({"schema_version": 99}, "schema_version"),
        ({"approved": False}, "local approval"),
        ({"approved_by": ""}, "approved_by"),
        ({"reason": ""}, "reason"),
        ({"assignment_id": "other-assignment"}, "assignment does not match"),
        ({"target": {"bundle_id": "other"}}, "target does not match"),
    ],
)
def test_apply_rejects_invalid_approval_records(
    tmp_path: Path,
    approval_update: dict[str, object],
    match: str,
) -> None:
    from mcp_broker.governance_pull import (
        GovernancePullError,
        apply_cached_bundle,
        pull_assigned_bundle,
    )

    bundle_path, decision = _assigned_bundle(tmp_path)
    state_dir = tmp_path / "state"
    pull_report = pull_assigned_bundle(
        source_url=bundle_path.as_uri(),
        assignment_decision=decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )
    approval = {**_approval(decision), **approval_update}

    with pytest.raises(GovernancePullError, match=match):
        apply_cached_bundle(
            pull_record_path=Path(str(pull_report["cache_record_path"])),
            state_dir=state_dir,
            approval_record=approval,
        )


def test_apply_rejects_missing_cached_bundle(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import (
        GovernancePullError,
        apply_cached_bundle,
        pull_assigned_bundle,
    )

    bundle_path, decision = _assigned_bundle(tmp_path)
    state_dir = tmp_path / "state"
    pull_report = pull_assigned_bundle(
        source_url=bundle_path.as_uri(),
        assignment_decision=decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )
    cache_record_path = Path(str(pull_report["cache_record_path"]))
    cache_record = json.loads(cache_record_path.read_text(encoding="utf-8"))
    Path(str(cache_record["cached_bundle_path"])).unlink()

    with pytest.raises(GovernancePullError, match="cached bundle not found"):
        apply_cached_bundle(
            pull_record_path=cache_record_path,
            state_dir=state_dir,
            approval_record=_approval(decision),
        )


def test_apply_reports_deployment_validation_errors(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import GovernancePullError, apply_cached_bundle

    state_dir = tmp_path / "state"
    bad_bundle = tmp_path / "bad-bundle.json"
    bad_bundle.write_text("{}", encoding="utf-8")
    pull_record = _write_json(
        tmp_path / "pull-record.json",
        {
            "assignment_id": "team-stable-ring",
            "cached_bundle_path": str(bad_bundle),
            "target": {
                "bundle_id": "personal-local",
                "version": "2026.07.01",
                "channel": "stable",
                "digest": {"algorithm": "sha256", "value": "0" * 64},
            },
        },
    )

    with pytest.raises(GovernancePullError, match="bundle"):
        apply_cached_bundle(
            pull_record_path=pull_record,
            state_dir=state_dir,
            approval_record={
                "schema_version": 1,
                "approved": True,
                "approved_by": "release-manager",
                "reason": "approve invalid bundle to prove fail closed",
                "assignment_id": "team-stable-ring",
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                    "digest": {"algorithm": "sha256", "value": "0" * 64},
                },
            },
        )


def test_rollback_delegates_to_transactional_deployment_state(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import (
        apply_cached_bundle,
        pull_assigned_bundle,
        rollback_governance_bundle,
    )

    state_dir = tmp_path / "state"
    first_bundle, first_decision = _assigned_bundle(tmp_path / "first", version="2026.07.01")
    second_bundle, second_decision = _assigned_bundle(tmp_path / "second", version="2026.07.02")

    first_pull = pull_assigned_bundle(
        source_url=first_bundle.as_uri(),
        assignment_decision=first_decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )
    apply_cached_bundle(
        pull_record_path=Path(str(first_pull["cache_record_path"])),
        state_dir=state_dir,
        approval_record=_approval(first_decision),
    )
    second_pull = pull_assigned_bundle(
        source_url=second_bundle.as_uri(),
        assignment_decision=second_decision,
        state_dir=state_dir,
        auth_ref=AUTH_REF,
        auth_present=True,
    )
    second_apply = apply_cached_bundle(
        pull_record_path=Path(str(second_pull["cache_record_path"])),
        state_dir=state_dir,
        approval_record=_approval(second_decision),
    )

    rollback = rollback_governance_bundle(state_dir)

    assert rollback["schema_version"] == 1
    assert rollback["action"] == "rollback"
    assert rollback["previous_deployment_id"] == second_apply["deployment_id"]
    active = json.loads((state_dir / "deployments" / "active.json").read_text(encoding="utf-8"))
    assert active["deployment_id"] == rollback["active_deployment_id"]


def test_rollback_reports_missing_previous_deployment(tmp_path: Path) -> None:
    from mcp_broker.governance_pull import GovernancePullError, rollback_governance_bundle

    with pytest.raises(GovernancePullError, match="active deployment pointer"):
        rollback_governance_bundle(tmp_path / "state")


def test_governance_pull_cli_reports_pull_apply_and_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_pull import main

    state_dir = tmp_path / "state"
    first_bundle, first_decision = _assigned_bundle(tmp_path / "first", version="2026.07.01")
    second_bundle, second_decision = _assigned_bundle(tmp_path / "second", version="2026.07.02")
    first_decision_path = _write_json(tmp_path / "first-decision.json", first_decision)
    second_decision_path = _write_json(tmp_path / "second-decision.json", second_decision)
    first_approval_path = _write_json(tmp_path / "first-approval.json", _approval(first_decision))
    second_approval_path = _write_json(tmp_path / "second-approval.json", _approval(second_decision))

    assert (
        main(
            [
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
            ]
        )
        == 0
    )
    first_pull_output = capsys.readouterr().out
    assert "governance bundle pulled:" in first_pull_output
    first_record = first_pull_output.rsplit("record=", maxsplit=1)[1].strip()

    assert (
        main(
            [
                "apply",
                "--pull-record",
                first_record,
                "--state-dir",
                str(state_dir),
                "--approval",
                str(first_approval_path),
            ]
        )
        == 0
    )
    assert "governance bundle applied:" in capsys.readouterr().out

    assert (
        main(
            [
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
            ]
        )
        == 0
    )
    second_record = capsys.readouterr().out.rsplit("record=", maxsplit=1)[1].strip()
    assert (
        main(
            [
                "apply",
                "--pull-record",
                second_record,
                "--state-dir",
                str(state_dir),
                "--approval",
                str(second_approval_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["rollback", "--state-dir", str(state_dir)]) == 0
    assert "governance bundle rolled back:" in capsys.readouterr().out


def test_governance_pull_cli_reports_json_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.governance_pull import main

    bundle_path, _decision = _assigned_bundle(tmp_path)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text("[]", encoding="utf-8")

    assert (
        main(
            [
                "pull",
                "--source",
                bundle_path.as_uri(),
                "--assignment-decision",
                str(decision_path),
                "--state-dir",
                str(tmp_path / "state"),
                "--auth-ref",
                AUTH_REF,
                "--auth-present",
            ]
        )
        == 1
    )
    assert "expected JSON object" in capsys.readouterr().out


@pytest.mark.error_simulation
def test_governance_pull_main_reports_unknown_dispatch_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.governance_pull as governance_pull

    monkeypatch.setattr(
        governance_pull,
        "_parse_args",
        lambda _argv: SimpleNamespace(governance_command="unknown"),
    )

    with pytest.raises(governance_pull.GovernancePullError, match="unknown governance command"):
        governance_pull.main([])


@pytest.mark.error_simulation
def test_governance_pull_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path, decision = _assigned_bundle(tmp_path)
    decision_path = _write_json(tmp_path / "decision.json", decision)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "governance_pull",
            "pull",
            "--source",
            bundle_path.as_uri(),
            "--assignment-decision",
            str(decision_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--auth-ref",
            AUTH_REF,
            "--auth-present",
        ],
    )

    module_name = "mcp_broker.governance_pull"
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


def _assigned_bundle(
    tmp_path: Path,
    *,
    bundle_id: str = "personal-local",
    version: str = "2026.07.01",
    channel: str = "stable",
) -> tuple[Path, dict[str, Any]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bundle = minimal_bundle()
    bundle["bundle_id"] = bundle_id
    bundle["version"] = version
    bundle["channel"] = channel
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)
    loaded = json.loads(bundle_path.read_text(encoding="utf-8"))
    return bundle_path, _assignment_decision(
        bundle_id=bundle_id,
        version=version,
        channel=channel,
        digest_value=str(loaded["checksum"]["value"]),
    )


def _assignment_decision(
    *,
    bundle_id: str,
    version: str,
    channel: str,
    digest_value: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "assignment_id": "team-stable-ring",
        "target": {
            "bundle_id": bundle_id,
            "version": version,
            "channel": channel,
            "digest": {
                "algorithm": "sha256",
                "value": digest_value,
            },
        },
        "changed_runtime_state": False,
    }


def _approval(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "approved": True,
        "approved_by": "release-manager",
        "reason": "approved governance bundle rollout",
        "assignment_id": decision["assignment_id"],
        "target": decision["target"],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path
