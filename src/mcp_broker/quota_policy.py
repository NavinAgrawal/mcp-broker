"""Shared-runtime quota and cost-control policy contracts."""

from __future__ import annotations

from typing import Any, Mapping

from mcp_broker.shared_runtime_policy import (
    SharedRuntimePolicyError,
    validate_tenant_context,
)


QUOTA_POLICY_SCHEMA_VERSION = 1
QUOTA_SCOPES = ("global", "team", "user", "upstream", "tool")


class QuotaPolicyError(ValueError):
    """Raised when quota policy input is unsafe."""


def build_quota_policy() -> dict[str, Any]:
    return {
        "schema_version": QUOTA_POLICY_SCHEMA_VERSION,
        "default_decision": "deny",
        "external_metering_supported": False,
        "enforced_scopes": list(QUOTA_SCOPES),
        "denial_audit_required": True,
    }


def decide_quota(
    *,
    tenant_context: Mapping[str, Any],
    team_id: str,
    upstream_id: str,
    tool_name: str,
    quota_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    context = _tenant_context(tenant_context)
    safe_team_id = _required_identifier(team_id, "team_id")
    safe_upstream_id = _required_identifier(upstream_id, "upstream_id")
    safe_tool_name = _required_identifier(tool_name, "tool_name")
    request_scope = {
        "tenant_id": context["tenant_id"],
        "workspace_id": context["workspace_id"],
        "user_id": context["user_id"],
        "team_id": safe_team_id,
        "upstream_id": safe_upstream_id,
        "tool_name": safe_tool_name,
    }
    denial = _first_denial(
        quota_snapshot=quota_snapshot,
        request_scope=request_scope,
    )
    if denial is not None:
        blocked_scope, reason = denial
        return {
            "allowed": False,
            "reason": reason,
            "blocked_scope": blocked_scope,
            "checked_scopes": list(QUOTA_SCOPES),
            "audit_event": _audit_event(
                request_scope=request_scope,
                result="denied",
                denial_reason=reason,
            ),
        }
    return {
        "allowed": True,
        "reason": "quota_allowed",
        "checked_scopes": list(QUOTA_SCOPES),
        "audit_event": _audit_event(request_scope=request_scope, result="allowed"),
    }


def _first_denial(
    *,
    quota_snapshot: Mapping[str, Any],
    request_scope: Mapping[str, str],
) -> tuple[str, str] | None:
    kill_switch_denial = _kill_switch_denial(
        quota_snapshot=quota_snapshot,
        request_scope=request_scope,
    )
    if kill_switch_denial is not None:
        return kill_switch_denial
    return _limit_denial(quota_snapshot=quota_snapshot, request_scope=request_scope)


def _kill_switch_denial(
    *,
    quota_snapshot: Mapping[str, Any],
    request_scope: Mapping[str, str],
) -> tuple[str, str] | None:
    kill_switches = quota_snapshot.get("kill_switches")
    if not isinstance(kill_switches, Mapping):
        return ("global", "global_kill_switch_missing")
    if kill_switches.get("global") is True:
        return ("global", "global_kill_switch")
    scoped_switches = (
        ("team", "teams", request_scope["team_id"]),
        ("user", "users", request_scope["user_id"]),
        ("upstream", "upstreams", request_scope["upstream_id"]),
        ("tool", "tools", request_scope["tool_name"]),
    )
    for scope, field, identifier in scoped_switches:
        values = kill_switches.get(field)
        if not isinstance(values, list):
            return (scope, f"{scope}_kill_switch_missing")
        if identifier in values:
            return (scope, f"{scope}_kill_switch")
    return None


def _limit_denial(
    *,
    quota_snapshot: Mapping[str, Any],
    request_scope: Mapping[str, str],
) -> tuple[str, str] | None:
    limits = quota_snapshot.get("limits")
    if not isinstance(limits, Mapping):
        return ("global", "global_quota_missing")
    limit_checks = (
        ("global", _limit_record(limits, "global")),
        ("team", _scoped_limit_record(limits, "teams", request_scope["team_id"])),
        ("user", _scoped_limit_record(limits, "users", request_scope["user_id"])),
        (
            "upstream",
            _scoped_limit_record(
                limits,
                "upstreams",
                request_scope["upstream_id"],
            ),
        ),
        ("tool", _scoped_limit_record(limits, "tools", request_scope["tool_name"])),
    )
    for scope, record in limit_checks:
        if record is None:
            return (scope, f"{scope}_quota_missing")
        if _used_count(record) >= _limit_count(record):
            return (scope, f"{scope}_quota_exceeded")
    return None


def _limit_record(limits: Mapping[str, Any], field: str) -> Mapping[str, Any] | None:
    record = limits.get(field)
    if isinstance(record, Mapping):
        return record
    return None


def _scoped_limit_record(
    limits: Mapping[str, Any],
    field: str,
    identifier: str,
) -> Mapping[str, Any] | None:
    scoped_limits = limits.get(field)
    if not isinstance(scoped_limits, Mapping):
        return None
    record = scoped_limits.get(identifier)
    if isinstance(record, Mapping):
        return record
    return None


def _limit_count(record: Mapping[str, Any]) -> int:
    value = record.get("limit")
    if not isinstance(value, int):
        return 0
    return max(0, value)


def _used_count(record: Mapping[str, Any]) -> int:
    value = record.get("used")
    if not isinstance(value, int):
        return 0
    return max(0, value)


def _audit_event(
    *,
    request_scope: Mapping[str, str],
    result: str,
    denial_reason: str | None = None,
) -> dict[str, str]:
    event = {
        "event_type": "quota_decision",
        "tenant_id": request_scope["tenant_id"],
        "workspace_id": request_scope["workspace_id"],
        "user_id": request_scope["user_id"],
        "team_id": request_scope["team_id"],
        "upstream_id": request_scope["upstream_id"],
        "tool_name": request_scope["tool_name"],
        "result": result,
    }
    if denial_reason is not None:
        event["denial_reason"] = denial_reason
    return event


def _tenant_context(context: Mapping[str, Any]) -> dict[str, str]:
    try:
        return validate_tenant_context(context)
    except SharedRuntimePolicyError as error:
        raise QuotaPolicyError(str(error)) from error


def _required_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuotaPolicyError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise QuotaPolicyError(f"{field} must not contain path separators")
    return value
