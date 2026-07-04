"""Hybrid local-edge and shared-worker routing contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from mcp_broker.broker import BrokerCore, BrokerToolError, ToolCaller
from mcp_broker.config import BrokerSettings, UpstreamConfig
from mcp_broker.profiles import ToolExposureProfile
from mcp_broker.quota_policy import QuotaPolicyError, decide_quota
from mcp_broker.shared_runtime_policy import LOCAL_EDGE_BOUNDARY, SHARED_WORKER_BOUNDARY


HYBRID_ROUTER_SCHEMA_VERSION = 1
STATELESS_TAG = "stateless"
SHARED_WORKER_TAGS = frozenset({"shared-worker", "shared_worker"})


class SharedWorkerCaller(Protocol):
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
        ...


@dataclass(frozen=True)
class SharedWorkerRoute:
    call_tool: Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class HybridRoutingContext:
    tenant_context: Mapping[str, Any] | None
    team_id: str | None
    quota_snapshot: Mapping[str, Any] | None

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "HybridRoutingContext":
        return cls(
            tenant_context=_optional_mapping(params.get("tenant_context")),
            team_id=_optional_string(params.get("team_id")),
            quota_snapshot=_optional_mapping(params.get("quota_snapshot")),
        )


class HybridToolRouter:
    def __init__(
        self,
        *,
        upstreams: Mapping[str, UpstreamConfig],
        shared_worker: SharedWorkerCaller | SharedWorkerRoute,
        settings: BrokerSettings | None = None,
        profile: ToolExposureProfile | None = None,
        call_locks: dict[str, Any] | None = None,
    ) -> None:
        self._settings = settings or BrokerSettings()
        self._profile = profile
        self._call_locks = {} if call_locks is None else call_locks
        self._upstreams = dict(upstreams)
        self._shared_worker = shared_worker

    def call_tool(
        self,
        *,
        advertised_name: str,
        arguments: dict[str, Any],
        edge_caller: ToolCaller,
        tenant_context: Mapping[str, Any] | None,
        team_id: str | None,
        quota_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        core = BrokerCore(
            settings=self._settings,
            upstreams=self._upstreams,
            profile=self._profile,
            call_locks=self._call_locks,
        )
        upstream, upstream_tool_name = core._resolve_call_route(advertised_name)
        if not self._should_try_shared_worker(
            upstream=upstream,
            tenant_context=tenant_context,
            team_id=team_id,
            quota_snapshot=quota_snapshot,
        ):
            return core.call_tool(advertised_name, arguments, edge_caller)
        quota_decision = _quota_decision(
            tenant_context=tenant_context,
            team_id=team_id,
            upstream_id=upstream.name,
            tool_name=advertised_name,
            quota_snapshot=quota_snapshot,
        )
        worker_response = self._shared_worker.call_tool(
            tenant_context=tenant_context,
            upstream_id=upstream.name,
            upstream_class=STATELESS_TAG,
            allowlisted=True,
            requires_local_state=False,
            tool_name=advertised_name,
            arguments=arguments,
            quota_decision=quota_decision,
        )
        if worker_response.get("allowed") is not True:
            return worker_response
        result = worker_response.get("result")
        if not isinstance(result, dict):
            raise BrokerToolError(
                code="invalid_shared_worker_response",
                message=f"invalid shared worker tools/call response from {upstream.name}",
                upstream_name=upstream.name,
                tool_name=upstream_tool_name,
            )
        BrokerCore._validate_tool_response(upstream.name, result)
        return result

    def _should_try_shared_worker(
        self,
        *,
        upstream: UpstreamConfig,
        tenant_context: Mapping[str, Any] | None,
        team_id: str | None,
        quota_snapshot: Mapping[str, Any] | None,
    ) -> bool:
        if tenant_context is None or team_id is None or quota_snapshot is None:
            return False
        tags = set(upstream.tags)
        return (
            STATELESS_TAG in tags
            and bool(tags & SHARED_WORKER_TAGS)
            and upstream.mode == "shared"
            and not upstream.mutating
            and not _requires_local_state(upstream)
        )


def build_hybrid_routing_policy() -> dict[str, Any]:
    return {
        "schema_version": HYBRID_ROUTER_SCHEMA_VERSION,
        "default_execution_boundary": LOCAL_EDGE_BOUNDARY,
        "client_contract": "unchanged_tools_call_shape",
        "shared_worker_boundary": SHARED_WORKER_BOUNDARY,
        "shared_worker_requires": [
            "stateless_tag",
            "shared_mode",
            "allowlisted",
            "no_local_state",
            "quota_allowed",
        ],
        "denial_audit_required": True,
    }


def _requires_local_state(upstream: UpstreamConfig) -> bool:
    return bool(
        upstream.state_dir
        or upstream.env
        or upstream.env_files
        or upstream.session_env
        or upstream.request_meta
    )


def _optional_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _quota_decision(
    *,
    tenant_context: Mapping[str, Any] | None,
    team_id: str | None,
    upstream_id: str,
    tool_name: str,
    quota_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if tenant_context is None or team_id is None or quota_snapshot is None:
        raise BrokerToolError(
            code="shared_routing_context_missing",
            message="shared routing requires tenant_context, team_id, and quota_snapshot",
            upstream_name=upstream_id,
            tool_name=tool_name,
        )
    try:
        return decide_quota(
            tenant_context=tenant_context,
            team_id=team_id,
            upstream_id=upstream_id,
            tool_name=tool_name,
            quota_snapshot=quota_snapshot,
        )
    except QuotaPolicyError as exc:
        raise BrokerToolError(
            code="shared_routing_quota_invalid",
            message=str(exc),
            upstream_name=upstream_id,
            tool_name=tool_name,
        ) from exc
