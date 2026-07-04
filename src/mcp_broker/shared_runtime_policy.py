"""Shared-runtime tenant and isolation policy contracts."""

from __future__ import annotations

from typing import Any, Mapping


SHARED_RUNTIME_POLICY_SCHEMA_VERSION = 1
LOCAL_EDGE_BOUNDARY = "local_edge"
SHARED_WORKER_BOUNDARY = "shared_worker"
REQUIRED_ISOLATION_DOMAINS = (
    "tenant",
    "workspace",
    "user",
    "upstream",
    "token",
    "log",
    "runtime_state",
    "audit",
)
LOCAL_ONLY_UPSTREAM_CLASSES = frozenset(
    {
        "browser",
        "file_access",
        "local_secret",
        "oauth",
        "stateful",
    }
)


class SharedRuntimePolicyError(ValueError):
    """Raised when shared-runtime policy input is unsafe."""


def build_shared_runtime_policy() -> dict[str, Any]:
    return {
        "schema_version": SHARED_RUNTIME_POLICY_SCHEMA_VERSION,
        "hosted_execution_supported": False,
        "default_execution_boundary": LOCAL_EDGE_BOUNDARY,
        "isolation_domains": list(REQUIRED_ISOLATION_DOMAINS),
        "tenant_context_required": ["tenant_id", "workspace_id", "user_id"],
        "upstream_defaults": {
            "unknown": LOCAL_EDGE_BOUNDARY,
            "stateful": LOCAL_EDGE_BOUNDARY,
            "stateless": LOCAL_EDGE_BOUNDARY,
        },
    }


def validate_shared_runtime_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    if policy.get("schema_version") != SHARED_RUNTIME_POLICY_SCHEMA_VERSION:
        raise SharedRuntimePolicyError("shared runtime policy schema_version is invalid")
    if policy.get("hosted_execution_supported") is not False:
        raise SharedRuntimePolicyError("hosted execution must remain unsupported")
    if policy.get("default_execution_boundary") != LOCAL_EDGE_BOUNDARY:
        raise SharedRuntimePolicyError("default execution boundary must be local_edge")
    domains = policy.get("isolation_domains")
    if tuple(domains or ()) != REQUIRED_ISOLATION_DOMAINS:
        raise SharedRuntimePolicyError("shared runtime isolation domains are incomplete")
    return policy


def validate_tenant_context(context: Mapping[str, Any]) -> dict[str, str]:
    return {
        "tenant_id": _required_identifier(context, "tenant_id"),
        "workspace_id": _required_identifier(context, "workspace_id"),
        "user_id": _required_identifier(context, "user_id"),
    }


def decide_upstream_placement(
    *,
    upstream_class: str,
    allowlisted: bool,
    requires_local_state: bool,
) -> dict[str, Any]:
    normalized_class = upstream_class.strip().lower()
    if (
        normalized_class == "stateless"
        and allowlisted
        and not requires_local_state
    ):
        return {
            "execution_boundary": SHARED_WORKER_BOUNDARY,
            "shared_worker_eligible": True,
            "reason": "allowlisted_stateless_upstream",
        }
    return {
        "execution_boundary": LOCAL_EDGE_BOUNDARY,
        "shared_worker_eligible": False,
        "reason": "local_state_or_unapproved_upstream_class",
    }


def _required_identifier(context: Mapping[str, Any], field: str) -> str:
    value = context.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SharedRuntimePolicyError(f"{field} is required")
    if "/" in value or "\\" in value:
        raise SharedRuntimePolicyError(f"{field} must not contain path separators")
    return value
