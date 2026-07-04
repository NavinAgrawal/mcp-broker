"""Phase 3 remote broker API contract validators."""

from __future__ import annotations

from typing import Any, Mapping

from mcp_broker.shared_runtime_policy import validate_tenant_context


REMOTE_API_SCHEMA_VERSION = 1
REMOTE_API_REQUEST_OPERATIONS = (
    "tool_discovery",
    "tool_describe",
    "tool_call",
    "status",
    "cancellation",
)
REMOTE_API_EVENT_TYPES = ("stream_chunk", "audit_event")
REMOTE_API_REQUIRED_CONTEXT = (
    "auth_context",
    "tenant_context",
    "policy_decision",
)


class RemoteApiContractError(ValueError):
    """Raised when a remote API contract payload is unsafe."""


def build_remote_api_contract() -> dict[str, Any]:
    return {
        "schema_version": REMOTE_API_SCHEMA_VERSION,
        "network_listener_supported": False,
        "request_operations": list(REMOTE_API_REQUEST_OPERATIONS),
        "event_types": list(REMOTE_API_EVENT_TYPES),
        "required_context": list(REMOTE_API_REQUIRED_CONTEXT),
    }


def validate_remote_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    operation = _required_choice(payload, "operation", REMOTE_API_REQUEST_OPERATIONS)
    request = {
        "operation": operation,
        "request_id": _required_identifier(payload, "request_id"),
        "tenant_context": _tenant_context(payload),
        "auth_context": _auth_context(payload),
        "policy_decision": _policy_decision(payload),
    }
    if operation in {"tool_describe", "tool_call"}:
        request["tool_name"] = _required_identifier(payload, "tool_name")
    if operation == "tool_call":
        request["arguments"] = _required_mapping(payload, "arguments")
    if operation == "cancellation":
        request["target_request_id"] = _required_identifier(
            payload,
            "target_request_id",
        )
    return request


def validate_stream_chunk(payload: Mapping[str, Any]) -> dict[str, Any]:
    event_type = _required_choice(payload, "event_type", ("stream_chunk",))
    sequence = payload.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        raise RemoteApiContractError("sequence must be a non-negative integer")
    final = payload.get("final")
    if not isinstance(final, bool):
        raise RemoteApiContractError("final must be a boolean")
    return {
        "event_type": event_type,
        "request_id": _required_identifier(payload, "request_id"),
        "sequence": sequence,
        "final": final,
        "payload": _required_mapping(payload, "payload"),
    }


def validate_audit_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    event_type = _required_choice(payload, "event_type", ("audit_event",))
    result = _required_choice(payload, "result", ("allowed", "denied", "failed"))
    event = {
        "event_type": event_type,
        "request_id": _required_identifier(payload, "request_id"),
        "tenant_id": _required_identifier(payload, "tenant_id"),
        "workspace_id": _required_identifier(payload, "workspace_id"),
        "user_id": _required_identifier(payload, "user_id"),
        "principal_id": _required_identifier(payload, "principal_id"),
        "action": _required_identifier(payload, "action"),
        "result": result,
    }
    if result == "denied":
        event["denial_reason"] = _required_identifier(payload, "denial_reason")
    return event


def _tenant_context(payload: Mapping[str, Any]) -> dict[str, str]:
    tenant_context = payload.get("tenant_context")
    if not isinstance(tenant_context, Mapping):
        raise RemoteApiContractError("tenant_context is required")
    return validate_tenant_context(tenant_context)


def _auth_context(payload: Mapping[str, Any]) -> dict[str, str]:
    auth_context = payload.get("auth_context")
    if not isinstance(auth_context, Mapping):
        raise RemoteApiContractError("auth_context is required")
    return {
        "principal_id": _required_identifier(auth_context, "principal_id"),
        "auth_method": _required_identifier(auth_context, "auth_method"),
    }


def _policy_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    policy_decision = payload.get("policy_decision")
    if not isinstance(policy_decision, Mapping):
        raise RemoteApiContractError("policy_decision is required")
    allowed = policy_decision.get("allowed")
    if allowed is not True:
        raise RemoteApiContractError("policy decision denied")
    return {
        "decision_id": _required_identifier(policy_decision, "decision_id"),
        "allowed": allowed,
        "reason": _required_identifier(policy_decision, "reason"),
    }


def _required_mapping(payload: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise RemoteApiContractError(f"{field} must be a mapping")
    return value


def _required_choice(
    payload: Mapping[str, Any],
    field: str,
    allowed_values: tuple[str, ...],
) -> str:
    value = _required_identifier(payload, field)
    if value not in allowed_values:
        raise RemoteApiContractError(f"{field} is not supported")
    return value


def _required_identifier(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RemoteApiContractError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise RemoteApiContractError(f"{field} must not contain path separators")
    return value
