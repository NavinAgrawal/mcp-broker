from __future__ import annotations

import json

import pytest


pytestmark = [pytest.mark.unit]

COLLECTION_TARGET = "https://collector.example.invalid/mcp-broker/fleet-status"
AUTH_REF = "env:MCP_BROKER_FLEET_COLLECTOR_TOKEN"
GENERATED_AT = "2026-07-02T10:00:00+00:00"
COLLECTOR_ID = "reference-fleet-collector"


def _redacted_status_payload() -> dict[str, object]:
    return {
        "identity": {
            "active_profiles": ["codex"],
            "broker_id": "reference-broker",
            "bundle_version": "bundle-2026.07.02",
            "environment": "local",
            "schema_version": 1,
        },
        "health": {
            "last_request_status": "ok",
            "started_at": "2026-07-02T09:59:00+00:00",
            "status": "running",
            "updated_at": "2026-07-02T10:00:00+00:00",
        },
        "request_counters": {
            "request_errors_total": 0,
            "requests_total": 7,
        },
        "upstreams": {
            "example-python": {
                "auth_state": "authenticated",
                "enabled": True,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "restarts": 0,
                "state": "running",
                "transport": "stdio",
            },
        },
    }


def _collection_status_payload() -> dict[str, object]:
    payload = _redacted_status_payload()
    payload["upstreams"] = {
        "upstream-001": payload["upstreams"]["example-python"],
    }
    return payload


def test_prepare_collection_envelope_adds_control_metadata_without_uploading() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    payload = _redacted_status_payload()

    envelope = prepare_collection_envelope(
        payload,
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    assert envelope == {
        "schema_version": 1,
        "kind": "mcp-broker.fleet-status.collection",
        "generated_at": GENERATED_AT,
        "collector": {
            "id": COLLECTOR_ID,
            "mode": "prepare-only",
        },
        "upload": {
            "target_url": COLLECTION_TARGET,
            "method": "POST",
            "attempted": False,
            "auth_ref": AUTH_REF,
        },
        "retention": {
            "days": 30,
            "delete_after": "2026-08-01T10:00:00+00:00",
        },
        "retry": {
            "max_attempts": 3,
            "backoff_seconds": 15,
        },
        "failure_handling": {
            "on_upload_failure": "mark_degraded",
            "local_spool": False,
        },
        "payload": _collection_status_payload(),
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_url": "https://collector.example.invalid/fleet?token=abc"}, "secret"),
        ({"target_url": "${HOME}/fleet-status.json"}, "target"),
        ({"auth_ref": "Bearer secret-token"}, "auth_ref"),
        ({"auth_ref": "env:"}, "auth_ref"),
        ({"retention_days": 0}, "retention"),
        ({"retention_days": 366}, "retention"),
    ],
)
def test_prepare_collection_envelope_rejects_unsafe_collection_controls(
    kwargs: dict[str, object],
    message: str,
) -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    args = {
        "status_payload": _redacted_status_payload(),
        "target_url": COLLECTION_TARGET,
        "auth_ref": AUTH_REF,
        "retention_days": 30,
        "generated_at": GENERATED_AT,
        "collector_id": COLLECTOR_ID,
    }
    args.update(kwargs)

    with pytest.raises(FleetCollectionError, match=message):
        prepare_collection_envelope(**args)


def test_prepare_collection_envelope_rejects_unredacted_status_payload() -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    unsafe_payload = _redacted_status_payload()
    unsafe_payload["local"] = {
        "socket_path": "${HOME}/mcp/mcp-broker/sockets/broker.sock",
        "pid": 321,
        "account": "engineer@example.com",
        "token": "secret-token",
    }

    with pytest.raises(FleetCollectionError, match="unsafe fleet status payload"):
        prepare_collection_envelope(
            unsafe_payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )


def test_collection_envelope_json_excludes_local_identity_and_secret_values() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    envelope = prepare_collection_envelope(
        _redacted_status_payload(),
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    rendered = json.dumps(envelope, sort_keys=True)
    assert "example-python" not in rendered
    assert "${HOME}" not in rendered
    assert "engineer@example.com" not in rendered
    assert "secret-token" not in rendered
    assert '"pid"' not in rendered
    assert "socket_path" not in rendered
    assert "client_secret" not in rendered
