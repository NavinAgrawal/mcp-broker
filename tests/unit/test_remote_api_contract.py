import pytest


pytestmark = pytest.mark.unit


def _base_request(operation: str = "tool_call") -> dict[str, object]:
    return {
        "operation": operation,
        "request_id": "request-001",
        "tenant_context": {
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
        },
        "auth_context": {
            "principal_id": "principal-a",
            "auth_method": "bearer",
        },
        "policy_decision": {
            "decision_id": "decision-001",
            "allowed": True,
            "reason": "policy_allowed",
        },
        "tool_name": "example.search",
        "arguments": {"query": "status"},
    }


def test_remote_api_contract_declares_phase_3_surface_without_listener() -> None:
    from mcp_broker.remote_api_contract import build_remote_api_contract

    contract = build_remote_api_contract()

    assert contract["schema_version"] == 1
    assert contract["network_listener_supported"] is False
    assert contract["request_operations"] == [
        "tool_discovery",
        "tool_describe",
        "tool_call",
        "status",
        "cancellation",
    ]
    assert contract["event_types"] == ["stream_chunk", "audit_event"]
    assert contract["required_context"] == [
        "auth_context",
        "tenant_context",
        "policy_decision",
    ]


def test_remote_api_request_validation_accepts_authenticated_tool_call() -> None:
    from mcp_broker.remote_api_contract import validate_remote_request

    request = validate_remote_request(_base_request())

    assert request["operation"] == "tool_call"
    assert request["request_id"] == "request-001"
    assert request["tenant_context"]["tenant_id"] == "tenant-a"
    assert request["auth_context"] == {
        "principal_id": "principal-a",
        "auth_method": "bearer",
    }
    assert request["policy_decision"]["allowed"] is True
    assert request["policy_decision"] == {
        "decision_id": "decision-001",
        "allowed": True,
        "reason": "policy_allowed",
    }
    assert request["tool_name"] == "example.search"
    assert request["arguments"] == {"query": "status"}


@pytest.mark.parametrize("operation", ["tool_discovery", "status"])
def test_remote_api_request_validation_accepts_context_only_operations(operation: str) -> None:
    from mcp_broker.remote_api_contract import validate_remote_request

    request_payload = _base_request(operation)
    request_payload.pop("tool_name")
    request_payload.pop("arguments")

    request = validate_remote_request(request_payload)

    assert request["operation"] == operation
    assert "tool_name" not in request
    assert "arguments" not in request


def test_remote_api_request_validation_accepts_tool_describe_without_arguments() -> None:
    from mcp_broker.remote_api_contract import validate_remote_request

    request_payload = _base_request("tool_describe")
    request_payload.pop("arguments")

    request = validate_remote_request(request_payload)

    assert request["operation"] == "tool_describe"
    assert request["tool_name"] == "example.search"
    assert "arguments" not in request


def test_remote_api_request_validation_accepts_cancellation_target() -> None:
    from mcp_broker.remote_api_contract import validate_remote_request

    request_payload = _base_request("cancellation")
    request_payload.pop("tool_name")
    request_payload.pop("arguments")
    request_payload["target_request_id"] = "request-000"

    request = validate_remote_request(request_payload)

    assert request["operation"] == "cancellation"
    assert request["target_request_id"] == "request-000"


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("auth_context", "auth_context is required"),
        ("tenant_context", "tenant_context is required"),
        ("policy_decision", "policy_decision is required"),
    ],
)
def test_remote_api_request_validation_rejects_missing_required_context(
    field: str,
    error: str,
) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request()
    request.pop(field)

    with pytest.raises(RemoteApiContractError, match=error):
        validate_remote_request(request)


@pytest.mark.parametrize(
    ("operation", "field", "error"),
    [
        ("tool_describe", "tool_name", "tool_name is required"),
        ("tool_call", "tool_name", "tool_name is required"),
        ("tool_call", "arguments", "arguments must be a mapping"),
        ("cancellation", "target_request_id", "target_request_id is required"),
    ],
)
def test_remote_api_request_validation_rejects_operation_shape_gaps(
    operation: str,
    field: str,
    error: str,
) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request(operation)
    request.pop(field, None)

    with pytest.raises(RemoteApiContractError, match=error):
        validate_remote_request(request)


def test_remote_api_request_validation_rejects_denied_policy_decision() -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request()
    request["policy_decision"] = {
        "decision_id": "decision-002",
        "allowed": False,
        "reason": "quota_denied",
    }

    with pytest.raises(RemoteApiContractError) as exc_info:
        validate_remote_request(request)
    assert str(exc_info.value) == "policy decision denied"


def test_remote_api_request_validation_requires_policy_decision_mapping() -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request()
    request["policy_decision"] = None

    with pytest.raises(RemoteApiContractError) as exc_info:
        validate_remote_request(request)
    assert str(exc_info.value) == "policy_decision is required"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("tenant_context", "tenant_context is required"),
        ("auth_context", "auth_context is required"),
    ],
)
def test_remote_api_request_validation_has_exact_context_errors(
    field: str,
    message: str,
) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request()
    request[field] = None

    with pytest.raises(RemoteApiContractError) as exc_info:
        validate_remote_request(request)
    assert str(exc_info.value) == message


def test_remote_api_request_validation_rejects_unsupported_operation() -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request("admin")

    with pytest.raises(RemoteApiContractError, match="operation is not supported"):
        validate_remote_request(request)


def test_remote_api_request_validation_rejects_path_separator_identifiers() -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_remote_request,
    )

    request = _base_request()
    request["request_id"] = "tenant/request"

    with pytest.raises(RemoteApiContractError, match="path separators"):
        validate_remote_request(request)


def test_stream_chunk_validation_requires_request_sequence_and_final_flag() -> None:
    from mcp_broker.remote_api_contract import validate_stream_chunk

    chunk = validate_stream_chunk(
        {
            "event_type": "stream_chunk",
            "request_id": "request-001",
            "sequence": 1,
            "final": False,
            "payload": {"delta": "ok"},
        }
    )

    assert chunk["event_type"] == "stream_chunk"
    assert chunk["request_id"] == "request-001"
    assert chunk["sequence"] == 1
    assert chunk["final"] is False
    assert chunk["payload"] == {"delta": "ok"}


def test_stream_chunk_validation_accepts_zero_as_first_sequence() -> None:
    from mcp_broker.remote_api_contract import validate_stream_chunk

    chunk = validate_stream_chunk(
        {
            "event_type": "stream_chunk",
            "request_id": "request-001",
            "sequence": 0,
            "final": True,
            "payload": {},
        }
    )

    assert chunk["sequence"] == 0


@pytest.mark.parametrize("sequence", [-1, "1", None])
def test_stream_chunk_validation_rejects_invalid_sequence(sequence: object) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_stream_chunk,
    )

    with pytest.raises(RemoteApiContractError) as exc_info:
        validate_stream_chunk(
            {
                "event_type": "stream_chunk",
                "request_id": "request-001",
                "sequence": sequence,
                "final": False,
                "payload": {"delta": "ok"},
            }
        )
    assert str(exc_info.value) == "sequence must be a non-negative integer"


@pytest.mark.parametrize("final", ["false", None, 0])
def test_stream_chunk_validation_rejects_invalid_final_flag(final: object) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_stream_chunk,
    )

    with pytest.raises(RemoteApiContractError) as exc_info:
        validate_stream_chunk(
            {
                "event_type": "stream_chunk",
                "request_id": "request-001",
                "sequence": 1,
                "final": final,
                "payload": {"delta": "ok"},
            }
        )
    assert str(exc_info.value) == "final must be a boolean"


def test_audit_event_validation_requires_actor_scope_and_result() -> None:
    from mcp_broker.remote_api_contract import validate_audit_event

    event = validate_audit_event(
        {
            "event_type": "audit_event",
            "request_id": "request-001",
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "principal_id": "principal-a",
            "action": "tool_call",
            "result": "denied",
            "denial_reason": "quota_denied",
        }
    )

    assert event == {
        "event_type": "audit_event",
        "request_id": "request-001",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "principal_id": "principal-a",
        "action": "tool_call",
        "result": "denied",
        "denial_reason": "quota_denied",
    }


def test_audit_event_validation_accepts_allowed_result_without_denial_reason() -> None:
    from mcp_broker.remote_api_contract import validate_audit_event

    event = validate_audit_event(
        {
            "event_type": "audit_event",
            "request_id": "request-001",
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "principal_id": "principal-a",
            "action": "tool_call",
            "result": "allowed",
        }
    )

    assert event["result"] == "allowed"
    assert "denial_reason" not in event


def test_audit_event_validation_accepts_failed_result() -> None:
    from mcp_broker.remote_api_contract import validate_audit_event

    event = validate_audit_event(
        {
            "event_type": "audit_event",
            "request_id": "request-001",
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "principal_id": "principal-a",
            "action": "tool_call",
            "result": "failed",
        }
    )

    assert event["result"] == "failed"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"event_type": "metric_event"}, "event_type is not supported"),
        ({"event_type": "audit_event", "result": "unknown"}, "result is not supported"),
        (
            {
                "event_type": "audit_event",
                "request_id": "request/001",
                "tenant_id": "tenant-a",
                "workspace_id": "workspace-a",
                "user_id": "user-a",
                "principal_id": "principal-a",
                "action": "tool_call",
                "result": "allowed",
            },
            "path separators",
        ),
    ],
)
def test_audit_event_validation_rejects_invalid_event_shape(
    payload: dict[str, object],
    message: str,
) -> None:
    from mcp_broker.remote_api_contract import (
        RemoteApiContractError,
        validate_audit_event,
    )

    with pytest.raises(RemoteApiContractError, match=message):
        validate_audit_event(payload)
