from __future__ import annotations

import json
from collections.abc import Callable

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


def test_prepare_collection_envelope_accepts_safe_target_query_params() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    envelope = prepare_collection_envelope(
        _redacted_status_payload(),
        target_url=f"{COLLECTION_TARGET}?ring=canary&region=us",
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    assert envelope["upload"]["target_url"] == f"{COLLECTION_TARGET}?ring=canary&region=us"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_url": "https://collector.example.invalid/fleet?token=abc"}, "secret"),
        ({"target_url": "https://collector.example.invalid/fleet?Token=abc"}, "secret"),
        ({"target_url": "https://collector.example.invalid/fleet?token="}, "secret"),
        ({"target_url": "https://collector.example.invalid/fleet?note=secret-token"}, "secret"),
        ({"target_url": "https://collector.example.invalid/fleet?note=APIKEY"}, "secret"),
        ({"target_url": "http://collector.example.invalid/fleet"}, "target"),
        ({"target_url": "https:///fleet"}, "target"),
        ({"target_url": "   "}, "target"),
        ({"target_url": "https://user:pass@collector.example.invalid/fleet"}, "secret credentials"),
        ({"target_url": "${HOME}/fleet-status.json"}, "target"),
        ({"target_url": None}, "target"),
        ({"auth_ref": "Bearer secret-token"}, "auth_ref"),
        ({"auth_ref": "env:"}, "auth_ref"),
        ({"auth_ref": "env: TOKEN"}, "auth_ref"),
        ({"auth_ref": "env::TOKEN"}, "auth_ref"),
        ({"auth_ref": "keychain:TOKEN:EXTRA"}, "auth_ref"),
        ({"auth_ref": "file:TOKEN"}, "auth_ref"),
        ({"auth_ref": None}, "auth_ref"),
        ({"retention_days": 0}, "retention"),
        ({"retention_days": 366}, "retention"),
        ({"retention_days": "30"}, "retention"),
        ({"retention_days": True}, "retention"),
        ({"generated_at": "not-a-date"}, "generated_at"),
        ({"generated_at": None}, "generated_at"),
        ({"collector_id": "collector/a"}, "collector_id"),
        ({"collector_id": "collector@example.com"}, "collector_id"),
        ({"collector_id": None}, "collector_id"),
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


@pytest.mark.parametrize("retention_days", [1, 365])
def test_prepare_collection_envelope_accepts_retention_boundaries(retention_days: int) -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    envelope = prepare_collection_envelope(
        _redacted_status_payload(),
        target_url=COLLECTION_TARGET,
        auth_ref="keychain:MCP_BROKER_FLEET_COLLECTOR_TOKEN",
        retention_days=retention_days,
        generated_at="2026-07-02T10:00:00Z",
        collector_id="collector:reference_1",
    )

    assert envelope["retention"]["days"] == retention_days
    assert envelope["upload"]["auth_ref"] == "keychain:MCP_BROKER_FLEET_COLLECTOR_TOKEN"
    assert envelope["collector"]["id"] == "collector:reference_1"


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


def test_prepare_collection_envelope_rejects_non_object_root_payload() -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    with pytest.raises(FleetCollectionError, match="root must be an object"):
        prepare_collection_envelope(
            [],
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("identity", [], "identity must be an object"),
        ("health", [], "health must be an object"),
        ("request_counters", [], "request_counters must be an object"),
        ("upstreams", [], "upstreams must be an object"),
    ],
)
def test_prepare_collection_envelope_rejects_non_object_payload_sections(
    field: str,
    value: object,
    message: str,
) -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    payload = _redacted_status_payload()
    payload[field] = value

    with pytest.raises(FleetCollectionError, match=message):
        prepare_collection_envelope(
            payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )


@pytest.mark.parametrize(
    ("field", "extra"),
    [
        ("identity", {"socket_path": "${HOME}/broker.sock"}),
        ("health", {"pid": 123}),
        ("request_counters", {"token": "secret-token"}),
    ],
)
def test_prepare_collection_envelope_rejects_disallowed_section_fields(
    field: str,
    extra: dict[str, object],
) -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    payload = _redacted_status_payload()
    section = payload[field]
    assert isinstance(section, dict)
    section.update(extra)

    with pytest.raises(FleetCollectionError, match="disallowed fields"):
        prepare_collection_envelope(
            payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )


def test_prepare_collection_envelope_rejects_unredacted_nested_status_value() -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    payload = _redacted_status_payload()
    upstreams = payload["upstreams"]
    assert isinstance(upstreams, dict)
    status = upstreams["example-python"]
    assert isinstance(status, dict)
    status["last_error"] = ["[redacted]", "token leaked"]

    with pytest.raises(FleetCollectionError, match="redaction required"):
        prepare_collection_envelope(
            payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )


def test_prepare_collection_envelope_accepts_redacted_nested_tuple_values() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    payload = _redacted_status_payload()
    upstreams = payload["upstreams"]
    assert isinstance(upstreams, dict)
    status = upstreams["example-python"]
    assert isinstance(status, dict)
    status["last_error"] = ("[redacted]", "[redacted]")

    envelope = prepare_collection_envelope(
        payload,
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    upstream_payload = envelope["payload"]["upstreams"]
    assert upstream_payload["upstream-001"]["last_error"] == ["[redacted]", "[redacted]"]


def test_prepare_collection_envelope_accepts_missing_optional_identity_section() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    payload = _redacted_status_payload()
    payload.pop("identity")

    envelope = prepare_collection_envelope(
        payload,
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    assert envelope["payload"]["identity"] == {}


def test_prepare_collection_envelope_redacts_nested_mapping_status_values() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    payload = _redacted_status_payload()
    upstreams = payload["upstreams"]
    assert isinstance(upstreams, dict)
    status = upstreams["example-python"]
    assert isinstance(status, dict)
    status["last_error"] = {"message": "[redacted]", "code": "auth_failed"}

    envelope = prepare_collection_envelope(
        payload,
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    assert envelope["payload"]["upstreams"]["upstream-001"]["last_error"] == {
        "code": "auth_failed",
        "message": "[redacted]",
    }


def test_prepare_collection_envelope_accepts_missing_optional_upstreams() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    payload = _redacted_status_payload()
    payload.pop("upstreams")

    envelope = prepare_collection_envelope(
        payload,
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=30,
        generated_at=GENERATED_AT,
        collector_id=COLLECTOR_ID,
    )

    assert envelope["payload"]["upstreams"] == {}


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


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"status_payload": []}, "unsafe fleet status payload: root must be an object"),
        ({"target_url": "   "}, "collection target must be an https URL"),
        ({"target_url": "http://collector.example.invalid/fleet"}, "collection target must be an https URL"),
        (
            {"target_url": "https://user@collector.example.invalid/fleet"},
            "collection target URL must not contain secret credentials",
        ),
        (
            {"target_url": "https://:pass@collector.example.invalid/fleet"},
            "collection target URL must not contain secret credentials",
        ),
        (
            {"target_url": "https://collector.example.invalid/fleet?token=value"},
            "collection target URL must not contain secret query data",
        ),
        ({"auth_ref": "Bearer value"}, "collection auth_ref must be env:NAME or keychain:NAME"),
        ({"auth_ref": "file:TOKEN"}, "collection auth_ref must be env:NAME or keychain:NAME"),
        ({"retention_days": 0}, "collection retention_days must be between 1 and 365"),
        ({"generated_at": None}, "collection generated_at must be ISO-8601"),
        ({"generated_at": "bad-date"}, "collection generated_at must be ISO-8601"),
        ({"collector_id": "bad/id"}, "collection collector_id must be a safe identifier"),
    ],
)
def test_collection_control_errors_are_stable(
    kwargs: dict[str, object],
    expected_message: str,
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

    with pytest.raises(FleetCollectionError) as exc_info:
        prepare_collection_envelope(**args)

    assert str(exc_info.value) == expected_message


def test_zulu_generated_at_controls_exact_retention_deadline() -> None:
    from mcp_broker.fleet_collection import prepare_collection_envelope

    envelope = prepare_collection_envelope(
        _redacted_status_payload(),
        target_url=COLLECTION_TARGET,
        auth_ref=AUTH_REF,
        retention_days=1,
        generated_at="2026-07-02T10:00:00Z",
        collector_id=COLLECTOR_ID,
    )

    assert envelope["retention"]["delete_after"] == "2026-07-03T10:00:00+00:00"


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "https://status.example.invalid/error",
        "/home/example/status.json",
        "engineer@example.com",
        "password expired",
    ],
)
def test_each_unsafe_status_string_class_is_rejected(unsafe_value: str) -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    payload = _redacted_status_payload()
    health = payload["health"]
    assert isinstance(health, dict)
    health["last_request_status"] = unsafe_value

    with pytest.raises(FleetCollectionError) as exc_info:
        prepare_collection_envelope(
            payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )

    assert str(exc_info.value) == "unsafe fleet status payload: redaction required"


@pytest.mark.parametrize(
    ("payload_mutator", "expected_message"),
    [
        (
            lambda payload: payload.update({"local": {"pid": 123}}),
            "unsafe fleet status payload: root contains disallowed fields",
        ),
        (
            lambda payload: payload["identity"].update({"pid": 123}),
            "unsafe fleet status payload: identity contains disallowed fields",
        ),
        (
            lambda payload: payload["upstreams"]["example-python"].update({"pid": 123}),
            "unsafe fleet status payload: upstreams contains disallowed fields",
        ),
        (
            lambda payload: payload.update({"upstreams": []}),
            "unsafe fleet status payload: upstreams must be an object",
        ),
    ],
)
def test_payload_structure_errors_name_the_rejected_section(
    payload_mutator: Callable[[dict[str, object]], None],
    expected_message: str,
) -> None:
    from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope

    payload = _redacted_status_payload()
    payload_mutator(payload)

    with pytest.raises(FleetCollectionError) as exc_info:
        prepare_collection_envelope(
            payload,
            target_url=COLLECTION_TARGET,
            auth_ref=AUTH_REF,
            retention_days=30,
            generated_at=GENERATED_AT,
            collector_id=COLLECTOR_ID,
        )

    assert str(exc_info.value) == expected_message
