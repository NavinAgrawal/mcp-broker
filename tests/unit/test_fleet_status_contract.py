from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path

import pytest

from mcp_broker.cli import main as cli_main


pytestmark = [pytest.mark.unit]


def _status_snapshot() -> dict[str, object]:
    return {
        "identity": {
            "active_profile": None,
            "active_profiles": ["codex", "ops"],
            "broker_id": "engineer-laptop",
            "bundle_version": "bundle-2026.07.01",
            "environment": "local",
            "schema_version": 1,
        },
        "break_glass": {
            "active_record": {
                "audit_path": "${HOME}/mcp/mcp-broker/state/break-glass/audit.jsonl",
                "bypassed_policy_paths": ["policy.rollout.approval"],
                "created_at": "2026-07-01T12:01:00Z",
                "expires_at": "2026-07-01T12:31:00Z",
                "operator": "engineer@example.com",
                "reason": "Emergency secret-token recovery",
                "record_id": "break-glass-example",
                "status": "active",
            },
            "degraded": True,
            "status": "active",
        },
        "last_request_method": "tools/call",
        "last_request_status": "ok",
        "pid": 321,
        "request_errors_total": 2,
        "requests_total": 22,
        "socket_path": "${HOME}/mcp/mcp-broker/sockets/broker.sock",
        "started_at": "2026-07-01T12:00:00+00:00",
        "status": "running",
        "updated_at": "2026-07-01T12:03:00+00:00",
        "upstreams": {
            "mail-prod": {
                "account": "engineer@example.com",
                "auth_state": "authenticated",
                "client_secret": "secret-value",
                "enabled": True,
                "env": {"TOKEN": "secret-token"},
                "last_error": "${HOME}/.config/token.json expired for engineer@example.com",
                "mode": "shared",
                "mutating": True,
                "pid": 654,
                "restarts": 1,
                "state": "running",
                "transport": "stdio",
            },
            "read-api": {
                "auth_state": "unknown",
                "enabled": True,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "pid": None,
                "restarts": 0,
                "state": "configured",
                "transport": "http",
                "url": "https://api.example.com/mcp",
            },
        },
    }


def test_export_fleet_status_redacts_local_and_secret_fields() -> None:
    from mcp_broker.fleet_status import export_fleet_status

    payload = export_fleet_status(_status_snapshot())

    assert payload == {
        "identity": {
            "active_profiles": ["codex", "ops"],
            "broker_id": "engineer-laptop",
            "bundle_version": "bundle-2026.07.01",
            "environment": "local",
            "schema_version": 1,
        },
        "health": {
            "last_request_status": "ok",
            "started_at": "2026-07-01T12:00:00+00:00",
            "status": "running",
            "updated_at": "2026-07-01T12:03:00+00:00",
        },
        "request_counters": {
            "request_errors_total": 2,
            "requests_total": 22,
        },
        "upstreams": {
            "mail-prod": {
                "auth_state": "authenticated",
                "enabled": True,
                "last_error": "[redacted]",
                "mode": "shared",
                "mutating": True,
                "restarts": 1,
                "state": "running",
                "transport": "stdio",
            },
            "read-api": {
                "auth_state": "unknown",
                "enabled": True,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "restarts": 0,
                "state": "configured",
                "transport": "http",
            },
        },
    }
    rendered = json.dumps(payload, sort_keys=True)
    assert "${HOME}" not in rendered
    assert "engineer@example.com" not in rendered
    assert "secret-value" not in rendered
    assert "secret-token" not in rendered
    assert "https://api.example.com" not in rendered
    assert "socket_path" not in rendered
    assert "pid" not in rendered
    assert '"env":' not in rendered
    assert "break_glass" not in rendered
    assert "break-glass-example" not in rendered


def test_fleet_status_cli_exports_redacted_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_file = tmp_path / "broker-status.json"
    status_file.write_text(json.dumps(_status_snapshot()), encoding="utf-8")

    assert cli_main(["fleet-status", "export", "--status-file", str(status_file)]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["identity"]["bundle_version"] == "bundle-2026.07.01"
    assert payload["request_counters"] == {
        "request_errors_total": 2,
        "requests_total": 22,
    }
    assert "${HOME}" not in output
    assert "engineer@example.com" not in output
    assert "secret" not in output


def test_fleet_status_direct_cli_wraps_collection_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.fleet_status import main

    status_file = tmp_path / "broker-status.json"
    status_file.write_text(json.dumps(_status_snapshot()), encoding="utf-8")

    assert (
        main(
            [
                "--status-file",
                str(status_file),
                "--target-url",
                "https://control-plane.example/fleet",
                "--auth-ref",
                "env:FLEET_TOKEN",
                "--retention-days",
                "7",
                "--generated-at",
                "2026-07-01T12:00:00+00:00",
                "--collector-id",
                "collector-a",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["upload"]["target_url"] == "https://control-plane.example/fleet"
    assert payload["upload"]["auth_ref"] == "env:FLEET_TOKEN"
    assert payload["collector"]["id"] == "collector-a"
    assert payload["payload"]["identity"]["broker_id"] == "engineer-laptop"


@pytest.mark.error_simulation
def test_fleet_status_collect_handler_omits_generated_at_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli_fleet_status

    calls: list[list[str]] = []
    monkeypatch.setattr(cli_fleet_status, "fleet_status_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli_fleet_status.handle_fleet_status_collect(
            Namespace(
                status_file=tmp_path / "broker-status.json",
                target_url="https://control-plane.example/fleet",
                auth_ref="env:FLEET_TOKEN",
                retention_days=7,
                collector_id="collector-a",
                generated_at=None,
            )
        )
        == 0
    )

    assert calls == [
        [
            "--status-file",
            str(tmp_path / "broker-status.json"),
            "--target-url",
            "https://control-plane.example/fleet",
            "--auth-ref",
            "env:FLEET_TOKEN",
            "--retention-days",
            "7",
            "--collector-id",
            "collector-a",
        ]
    ]


def test_fleet_status_direct_cli_reports_collection_errors(tmp_path: Path) -> None:
    from mcp_broker.fleet_status import main

    status_file = tmp_path / "broker-status.json"
    status_file.write_text(json.dumps(_status_snapshot()), encoding="utf-8")

    with pytest.raises(SystemExit, match="auth_ref"):
        main(
            [
                "--status-file",
                str(status_file),
                "--target-url",
                "https://control-plane.example/fleet",
            ]
        )


def test_export_fleet_status_redacts_tuple_values() -> None:
    from mcp_broker.fleet_status import export_fleet_status

    snapshot = {
        "identity": {"broker_id": "broker-a"},
        "upstreams": {
            "mail": {
                "last_error": ("token expired", "engineer@example.com"),
                "enabled": True,
            }
        },
    }

    payload = export_fleet_status(snapshot)

    assert payload["upstreams"]["mail"]["last_error"] == ["[redacted]", "[redacted]"]


def test_export_fleet_status_redacts_nested_mapping_values() -> None:
    from mcp_broker.fleet_status import export_fleet_status

    snapshot = {
        "identity": {"broker_id": "broker-a"},
        "upstreams": {
            "mail": {
                "last_error": {
                    "message": "token expired",
                    "detail": "engineer@example.com",
                },
                "enabled": True,
            }
        },
    }

    payload = export_fleet_status(snapshot)

    assert payload["upstreams"]["mail"]["last_error"] == {
        "detail": "[redacted]",
        "message": "[redacted]",
    }


def test_fleet_status_parser_exposes_description_and_requires_status_file() -> None:
    from mcp_broker.fleet_status import _parser

    parser = _parser()

    assert parser.description == "Export a redacted fleet-status payload"
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])
    assert exc_info.value.code == 2


def test_fleet_status_current_timestamp_requests_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timezone

    from mcp_broker import fleet_status

    received_timezones: list[object] = []

    class FakeTimestamp:
        def isoformat(self) -> str:
            return "2026-07-01T12:00:00+00:00"

    class RecordingDatetime:
        @staticmethod
        def now(selected_timezone: object) -> FakeTimestamp:
            received_timezones.append(selected_timezone)
            return FakeTimestamp()

    monkeypatch.setattr(fleet_status, "datetime", RecordingDatetime)

    assert fleet_status._current_timestamp() == "2026-07-01T12:00:00+00:00"
    assert received_timezones == [timezone.utc]


def test_fleet_status_direct_cli_uses_current_timestamp_when_not_supplied(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import fleet_status

    status_file = tmp_path / "broker-status.json"
    status_file.write_text(json.dumps(_status_snapshot()), encoding="utf-8")
    monkeypatch.setattr(
        fleet_status,
        "_current_timestamp",
        lambda: "2026-07-01T12:00:00+00:00",
    )

    assert (
        fleet_status.main(
            [
                "--status-file",
                str(status_file),
                "--target-url",
                "https://control-plane.example/fleet",
                "--auth-ref",
                "env:FLEET_TOKEN",
                "--retention-days",
                "7",
                "--collector-id",
                "collector-a",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["generated_at"] == (
        "2026-07-01T12:00:00+00:00"
    )


def test_fleet_status_direct_cli_emits_stably_sorted_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.fleet_status import main

    status_file = tmp_path / "broker-status.json"
    status_file.write_text(
        json.dumps({"status": "running", "identity": {"broker_id": "broker-a"}}),
        encoding="utf-8",
    )

    assert main(["--status-file", str(status_file)]) == 0

    assert capsys.readouterr().out == (
        '{"health": {"status": "running"}, "identity": {"broker_id": "broker-a"}, '
        '"request_counters": {}, "upstreams": {}}\n'
    )


def test_fleet_status_sensitive_string_detection_flags_urls() -> None:
    from mcp_broker.fleet_status import _is_sensitive_status_string

    assert _is_sensitive_status_string("https://api.example.com/status") is True
    assert _is_sensitive_status_string("healthy") is False
