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
    assert request["tenant_context"]["tenant_id"] == "tenant-a"
    assert request["auth_context"]["principal_id"] == "principal-a"
    assert request["policy_decision"]["allowed"] is True
    assert request["tool_name"] == "example.search"
    assert request["arguments"] == {"query": "status"}


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

    with pytest.raises(RemoteApiContractError, match="policy decision denied"):
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

    assert event["event_type"] == "audit_event"
    assert event["tenant_id"] == "tenant-a"
    assert event["principal_id"] == "principal-a"
    assert event["action"] == "tool_call"
    assert event["result"] == "denied"
    assert event["denial_reason"] == "quota_denied"
