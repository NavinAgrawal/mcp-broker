"""In-process shared-worker isolation proof for stateless fake tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from mcp_broker.session_affinity import SessionAffinityError, decide_session_affinity
from mcp_broker.shared_runtime_policy import (
    SHARED_WORKER_BOUNDARY,
    SharedRuntimePolicyError,
    validate_tenant_context,
)


SHARED_WORKER_SCHEMA_VERSION = 1
SUPPORTED_TOOL_BEHAVIORS = frozenset({"echo", "inspect_sandbox"})


class SharedWorkerError(ValueError):
    """Raised when a shared-worker request is unsafe."""


@dataclass(frozen=True)
class SharedWorkerTool:
    upstream_id: str
    name: str
    behavior: str


@dataclass(frozen=True)
class _WorkerSandbox:
    environment: Mapping[str, str]
    secret_names: tuple[str, ...]
    file_roots: tuple[str, ...]
    network_allowed: bool
    local_state_allowed: bool

    @classmethod
    def default_deny(cls) -> "_WorkerSandbox":
        return cls(
            environment={},
            secret_names=(),
            file_roots=(),
            network_allowed=False,
            local_state_allowed=False,
        )

    def describe(self) -> dict[str, object]:
        return {
            "environment": dict(self.environment),
            "secret_names": list(self.secret_names),
            "file_roots": list(self.file_roots),
            "network_allowed": self.network_allowed,
            "local_state_allowed": self.local_state_allowed,
        }


class SharedWorkerRuntime:
    def __init__(self, *, tools: list[SharedWorkerTool]) -> None:
        self._tools = {_tool_key(tool.upstream_id, tool.name): _validate_tool(tool) for tool in tools}

    def call_tool(
        self,
        *,
        tenant_context: Mapping[str, Any],
        upstream_id: str,
        upstream_class: str,
        allowlisted: bool,
        requires_local_state: bool,
        tool_name: str,
        arguments: Mapping[str, Any],
        quota_decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = _tenant_context(tenant_context)
        safe_upstream_id = _required_identifier(upstream_id, "upstream_id")
        safe_tool_name = _required_tool_name(tool_name)
        worker_scope = {
            "tenant_id": context["tenant_id"],
            "workspace_id": context["workspace_id"],
            "user_id": context["user_id"],
            "upstream_id": safe_upstream_id,
        }
        placement_denial = _placement_denial(
            upstream_class=upstream_class,
            upstream_id=safe_upstream_id,
            allowlisted=allowlisted,
            requires_local_state=requires_local_state,
            tenant_context=context,
        )
        if placement_denial is not None:
            return _denied_decision(
                worker_scope=worker_scope,
                tool_name=safe_tool_name,
                reason=placement_denial,
            )
        quota_denial = _quota_denial(quota_decision)
        if quota_denial is not None:
            return _denied_decision(
                worker_scope=worker_scope,
                tool_name=safe_tool_name,
                reason="quota_denied",
                audit_reason=quota_denial,
            )
        tool = self._tools.get(_tool_key(safe_upstream_id, safe_tool_name))
        if tool is None:
            return _denied_decision(
                worker_scope=worker_scope,
                tool_name=safe_tool_name,
                reason="unsupported_tool",
            )
        result = _execute_fake_tool(
            tool=tool,
            arguments=arguments,
            sandbox=_WorkerSandbox.default_deny(),
        )
        return {
            "allowed": True,
            "reason": "worker_call_allowed",
            "worker_scope": worker_scope,
            "result": result,
            "audit_event": _audit_event(
                worker_scope=worker_scope,
                tool_name=safe_tool_name,
                result="allowed",
            ),
        }

    def state_snapshot(self) -> dict[str, object]:
        return {}


def build_shared_worker_policy() -> dict[str, Any]:
    return {
        "schema_version": SHARED_WORKER_SCHEMA_VERSION,
        "runtime_kind": "in_process_fake_worker",
        "real_upstream_routing_supported": False,
        "eligible_upstream_classes": ["stateless"],
        "required_placement": SHARED_WORKER_BOUNDARY,
        "default_capabilities": {
            "network": "deny",
            "file_access": "deny",
            "secrets": "deny",
            "local_state": "deny",
            "inherited_environment": "deny",
        },
        "denial_audit_required": True,
    }


def _execute_fake_tool(
    *,
    tool: SharedWorkerTool,
    arguments: Mapping[str, Any],
    sandbox: _WorkerSandbox,
) -> dict[str, Any]:
    if tool.behavior == "echo":
        message = arguments.get("message")
        if not isinstance(message, str):
            message = ""
        return {
            "content": [{"type": "text", "text": message}],
            "structuredContent": {"message": message},
        }
    if tool.behavior == "inspect_sandbox":
        return {
            "content": [{"type": "text", "text": "sandbox"}],
            "structuredContent": sandbox.describe(),
        }
    raise SharedWorkerError("unsupported shared worker tool behavior")


def _placement_denial(
    *,
    upstream_class: str,
    upstream_id: str,
    allowlisted: bool,
    requires_local_state: bool,
    tenant_context: Mapping[str, Any],
) -> str | None:
    try:
        placement = decide_session_affinity(
            upstream_class=upstream_class,
            upstream_id=upstream_id,
            allowlisted=allowlisted,
            requires_local_state=requires_local_state,
            tenant_context=tenant_context,
        )
    except SessionAffinityError as error:
        raise SharedWorkerError(str(error)) from error
    if placement["execution_boundary"] != SHARED_WORKER_BOUNDARY:
        return "shared_worker_placement_denied"
    return None


def _quota_denial(quota_decision: Mapping[str, Any]) -> str | None:
    if quota_decision.get("allowed") is True:
        return None
    reason = quota_decision.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return "quota_denied"


def _denied_decision(
    *,
    worker_scope: Mapping[str, str],
    tool_name: str,
    reason: str,
    audit_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "allowed": False,
        "reason": reason,
        "worker_scope": dict(worker_scope),
        "result": None,
        "audit_event": _audit_event(
            worker_scope=worker_scope,
            tool_name=tool_name,
            result="denied",
            denial_reason=audit_reason or reason,
        ),
    }


def _audit_event(
    *,
    worker_scope: Mapping[str, str],
    tool_name: str,
    result: str,
    denial_reason: str | None = None,
) -> dict[str, str]:
    event = {
        "event_type": "shared_worker_call",
        "tenant_id": worker_scope["tenant_id"],
        "workspace_id": worker_scope["workspace_id"],
        "user_id": worker_scope["user_id"],
        "upstream_id": worker_scope["upstream_id"],
        "tool_name": tool_name,
        "result": result,
    }
    if denial_reason is not None:
        event["denial_reason"] = denial_reason
    return event


def _validate_tool(tool: SharedWorkerTool) -> SharedWorkerTool:
    _required_identifier(tool.upstream_id, "upstream_id")
    _required_tool_name(tool.name)
    if tool.behavior not in SUPPORTED_TOOL_BEHAVIORS:
        raise SharedWorkerError("unsupported shared worker tool behavior")
    return tool


def _tool_key(upstream_id: str, tool_name: str) -> tuple[str, str]:
    return (upstream_id, tool_name)


def _tenant_context(context: Mapping[str, Any]) -> dict[str, str]:
    try:
        return validate_tenant_context(context)
    except SharedRuntimePolicyError as error:
        raise SharedWorkerError(str(error)) from error


def _required_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SharedWorkerError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise SharedWorkerError(f"{field} must not contain path separators")
    return value


def _required_tool_name(value: str) -> str:
    _required_identifier(value, "tool_name")
    if "." not in value:
        raise SharedWorkerError("tool_name must include upstream prefix")
    return value
