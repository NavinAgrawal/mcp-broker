"""Shared-runtime session affinity and state-placement contracts."""

from __future__ import annotations

from typing import Any, Mapping

from mcp_broker.shared_runtime_policy import (
    LOCAL_EDGE_BOUNDARY,
    LOCAL_ONLY_UPSTREAM_CLASSES,
    SHARED_WORKER_BOUNDARY,
    SharedRuntimePolicyError,
    decide_upstream_placement,
    validate_tenant_context,
)


SESSION_AFFINITY_SCHEMA_VERSION = 1
UNKNOWN_UPSTREAM_CLASS = "unknown"
STATELESS_UPSTREAM_CLASS = "stateless"
FORBIDDEN_UPSTREAM_CLASSES = frozenset({"private_inventory"})


class SessionAffinityError(ValueError):
    """Raised when a session affinity decision would be unsafe."""


def build_session_affinity_policy() -> dict[str, Any]:
    return {
        "schema_version": SESSION_AFFINITY_SCHEMA_VERSION,
        "default_execution_boundary": LOCAL_EDGE_BOUNDARY,
        "local_only_upstream_classes": [
            "browser",
            "file_access",
            "local_secret",
            "oauth",
            "stateful",
            UNKNOWN_UPSTREAM_CLASS,
        ],
        "shared_eligible_upstream_classes": [STATELESS_UPSTREAM_CLASS],
        "forbidden_upstream_classes": ["private_inventory"],
    }


def decide_session_affinity(
    *,
    upstream_class: str,
    upstream_id: str,
    allowlisted: bool,
    requires_local_state: bool,
    tenant_context: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_class = _normalize_upstream_class(upstream_class)
    if normalized_class in FORBIDDEN_UPSTREAM_CLASSES:
        raise SessionAffinityError("upstream class is forbidden")
    placement = decide_upstream_placement(
        upstream_class=normalized_class,
        allowlisted=allowlisted,
        requires_local_state=requires_local_state,
    )
    safe_upstream_id = _required_identifier(upstream_id, "upstream_id")
    if placement["execution_boundary"] == SHARED_WORKER_BOUNDARY:
        context = _tenant_context(tenant_context)
        return {
            "upstream_class": normalized_class,
            "execution_boundary": SHARED_WORKER_BOUNDARY,
            "session_affinity": "tenant_workspace_user",
            "state_binding": "shared_worker_scope",
            "shared_worker_eligible": True,
            "reason": placement["reason"],
            "state_scope": {
                "tenant_id": context["tenant_id"],
                "workspace_id": context["workspace_id"],
                "user_id": context["user_id"],
                "upstream_id": safe_upstream_id,
            },
        }
    return {
        "upstream_class": normalized_class,
        "execution_boundary": LOCAL_EDGE_BOUNDARY,
        "session_affinity": "local_client_session",
        "state_binding": "local_edge_session",
        "shared_worker_eligible": False,
        "reason": placement["reason"],
        "state_scope": {
            "session": "local_client_session",
            "upstream_id": safe_upstream_id,
        },
    }


def _normalize_upstream_class(upstream_class: str) -> str:
    normalized = upstream_class.strip().lower()
    if not normalized:
        return UNKNOWN_UPSTREAM_CLASS
    if normalized in LOCAL_ONLY_UPSTREAM_CLASSES:
        return normalized
    if normalized == STATELESS_UPSTREAM_CLASS:
        return normalized
    if normalized in FORBIDDEN_UPSTREAM_CLASSES:
        return normalized
    return UNKNOWN_UPSTREAM_CLASS


def _tenant_context(context: Mapping[str, Any]) -> dict[str, str]:
    try:
        return validate_tenant_context(context)
    except SharedRuntimePolicyError as error:
        raise SessionAffinityError(str(error)) from error


def _required_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionAffinityError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise SessionAffinityError(f"{field} must not contain path separators")
    return value
