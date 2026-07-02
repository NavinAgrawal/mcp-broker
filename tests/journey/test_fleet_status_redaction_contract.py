from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_broker.cli import main as cli_main


pytestmark = [pytest.mark.journey]

COLLECTION_TARGET = "https://collector.example.invalid/mcp-broker/fleet-status"
AUTH_REF = "env:MCP_BROKER_FLEET_COLLECTOR_TOKEN"
GENERATED_AT = "2026-07-02T10:00:00+00:00"
COLLECTOR_ID = "reference-fleet-collector"


def _status_snapshot() -> dict[str, object]:
    return {
        "identity": {
            "active_profiles": ["codex", "agy"],
            "broker_id": "engineer-laptop",
            "bundle_version": "bundle-2026.07.02",
            "environment": "local",
            "schema_version": 1,
        },
        "last_request_status": "ok",
        "pid": 321,
        "request_errors_total": 1,
        "requests_total": 12,
        "socket_path": "${HOME}/mcp/mcp-broker/sockets/broker.sock",
        "started_at": "2026-07-02T09:58:00+00:00",
        "status": "running",
        "updated_at": "2026-07-02T10:00:00+00:00",
        "upstreams": {
            "example-python": {
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
            "example-http": {
                "auth_state": "unknown",
                "enabled": True,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "pid": None,
                "restarts": 0,
                "state": "configured",
                "transport": "http",
                "url": "https://api.example.invalid/mcp?token=secret-token",
            },
        },
    }


def test_fleet_status_collect_cli_outputs_redacted_collection_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_file = tmp_path / "broker-status.json"
    status_file.write_text(json.dumps(_status_snapshot()), encoding="utf-8")

    assert (
        cli_main(
            [
                "fleet-status",
                "collect",
                "--status-file",
                str(status_file),
                "--target-url",
                COLLECTION_TARGET,
                "--auth-ref",
                AUTH_REF,
                "--retention-days",
                "30",
                "--generated-at",
                GENERATED_AT,
                "--collector-id",
                COLLECTOR_ID,
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    envelope = json.loads(output)
    assert envelope["kind"] == "mcp-broker.fleet-status.collection"
    assert envelope["upload"] == {
        "target_url": COLLECTION_TARGET,
        "method": "POST",
        "attempted": False,
        "auth_ref": AUTH_REF,
    }
    assert envelope["collector"] == {
        "id": COLLECTOR_ID,
        "mode": "prepare-only",
    }
    assert envelope["retention"]["days"] == 30
    assert envelope["failure_handling"]["on_upload_failure"] == "mark_degraded"
    upstream_statuses = envelope["payload"]["upstreams"]
    assert sorted(upstream_statuses) == ["upstream-001", "upstream-002"]
    assert any(status["last_error"] == "[redacted]" for status in upstream_statuses.values())

    assert "example-python" not in output
    assert "example-http" not in output
    assert "${HOME}" not in output
    assert "engineer@example.com" not in output
    assert "secret-value" not in output
    assert "secret-token" not in output
    assert "https://api.example.invalid" not in output
    assert '"pid"' not in output
    assert "socket_path" not in output
    assert '"env":' not in output
