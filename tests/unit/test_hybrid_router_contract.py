from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}

ALLOWED_QUOTA = {"allowed": True, "reason": "quota_allowed"}


def test_hybrid_router_policy_declares_default_local_boundary() -> None:
    from mcp_broker.hybrid_router import build_hybrid_routing_policy

    policy = build_hybrid_routing_policy()

    assert policy["schema_version"] == 1
    assert policy["default_execution_boundary"] == "local_edge"
    assert policy["client_contract"] == "unchanged_tools_call_shape"
    assert policy["shared_worker_boundary"] == "shared_worker"
    assert policy["shared_worker_requires"] == [
        "stateless_tag",
        "shared_mode",
        "allowlisted",
        "no_local_state",
        "quota_allowed",
    ]
    assert policy["denial_audit_required"] is True


def test_hybrid_routing_context_ignores_invalid_optional_params() -> None:
    from mcp_broker.hybrid_router import HybridRoutingContext, _optional_string

    context = HybridRoutingContext.from_params(
        {
            "tenant_context": TENANT_CONTEXT,
            "team_id": ["not-a-string"],
            "quota_snapshot": "not-a-map",
        }
    )

    assert context.tenant_context == TENANT_CONTEXT
    assert context.team_id is None
    assert context.quota_snapshot is None
    assert _optional_string("team-a") == "team-a"


def test_local_only_upstream_routes_to_edge_without_calling_shared_worker() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    edge = RecordingEdgeCaller()
    worker = RecordingSharedWorker()
    router = HybridToolRouter(
        upstreams={
            "local-store": UpstreamConfig(
                name="local-store",
                command="local-store",
                tool_prefix="local-store",
                mode="per_session",
                tags=("stateful",),
                state_dir="local-store",
            )
        },
        shared_worker=SharedWorkerRoute(worker.call_tool),
    )

    result = router.call_tool(
        advertised_name="local-store.search",
        arguments={"query": "refund"},
        edge_caller=edge.call,
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(),
    )

    assert result == {
        "content": [{"type": "text", "text": "edge:local-store.search"}],
        "structuredContent": {"execution_boundary": "local_edge"},
    }
    assert edge.calls == [("local-store", "search", {"query": "refund"}, 60)]
    assert worker.calls == []


def test_allowlisted_stateless_tool_routes_to_shared_worker_with_same_request_shape() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    edge = RecordingEdgeCaller()
    worker = RecordingSharedWorker()
    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(worker.call_tool),
    )

    result = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "hello"},
        edge_caller=edge.call,
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(tool_name="example.echo"),
    )

    assert result == {
        "content": [{"type": "text", "text": "worker:example.echo"}],
        "structuredContent": {
            "execution_boundary": "shared_worker",
            "arguments": {"message": "hello"},
        },
    }
    assert edge.calls == []
    assert worker.calls == [
        {
            "tenant_context": TENANT_CONTEXT,
            "upstream_id": "example-stateless",
            "upstream_class": "stateless",
            "allowlisted": True,
            "requires_local_state": False,
            "tool_name": "example.echo",
            "arguments": {"message": "hello"},
            "quota_decision": {
                "allowed": True,
                "reason": "quota_allowed",
                "checked_scopes": ["global", "team", "user", "upstream", "tool"],
                "audit_event": {
                    "event_type": "quota_decision",
                    "tenant_id": "tenant-a",
                    "workspace_id": "workspace-a",
                    "user_id": "user-a",
                    "team_id": "team-a",
                    "upstream_id": "example-stateless",
                    "tool_name": "example.echo",
                    "result": "allowed",
                },
            },
        }
    ]


def test_stateless_upstream_without_allowlist_stays_local_edge() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    edge = RecordingEdgeCaller()
    worker = RecordingSharedWorker()
    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless",),
            )
        },
        shared_worker=SharedWorkerRoute(worker.call_tool),
    )

    result = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "local"},
        edge_caller=edge.call,
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(tool_name="example.echo"),
    )

    assert result["structuredContent"] == {"execution_boundary": "local_edge"}
    assert edge.calls == [("example-stateless", "echo", {"message": "local"}, 60)]
    assert worker.calls == []


def test_shared_eligible_quota_denial_fails_closed_without_edge_fallback() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    edge = RecordingEdgeCaller()
    worker = RecordingSharedWorker()
    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(worker.call_tool),
    )

    result = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "blocked"},
        edge_caller=edge.call,
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tool_name="example.echo",
            tool_limit=0,
            tool_used=0,
        ),
    )

    assert result["allowed"] is False
    assert result["reason"] == "quota_denied"
    assert result["audit_event"]["denial_reason"] == "tool_quota_exceeded"
    assert edge.calls == []
    assert worker.calls == [
        {
            "tenant_context": TENANT_CONTEXT,
            "upstream_id": "example-stateless",
            "upstream_class": "stateless",
            "allowlisted": True,
            "requires_local_state": False,
            "tool_name": "example.echo",
            "arguments": {"message": "blocked"},
            "quota_decision": {
                "allowed": False,
                "reason": "tool_quota_exceeded",
                "blocked_scope": "tool",
                "checked_scopes": ["global", "team", "user", "upstream", "tool"],
                "audit_event": {
                    "event_type": "quota_decision",
                    "tenant_id": "tenant-a",
                    "workspace_id": "workspace-a",
                    "user_id": "user-a",
                    "team_id": "team-a",
                    "upstream_id": "example-stateless",
                    "tool_name": "example.echo",
                    "result": "denied",
                    "denial_reason": "tool_quota_exceeded",
                },
            },
        }
    ]


def test_unknown_tool_prefix_fails_before_routing() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    router = HybridToolRouter(upstreams={}, shared_worker=SharedWorkerRoute(lambda **_: {}))

    with pytest.raises(BrokerToolError, match="unknown tool prefix: missing"):
        router.call_tool(
            advertised_name="missing.search",
            arguments={},
            edge_caller=RecordingEdgeCaller().call,
            tenant_context=TENANT_CONTEXT,
            team_id="team-a",
            quota_snapshot=_quota_snapshot(),
        )


def test_shared_worker_invalid_result_fails_closed() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(lambda **_kwargs: {"allowed": True, "result": "not-a-map"}),
    )

    with pytest.raises(BrokerToolError) as exc_info:
        router.call_tool(
            advertised_name="example.echo",
            arguments={},
            edge_caller=RecordingEdgeCaller().call,
            tenant_context=TENANT_CONTEXT,
            team_id="team-a",
            quota_snapshot=_quota_snapshot(tool_name="example.echo"),
        )
    assert exc_info.value.code == "invalid_shared_worker_response"
    assert exc_info.value.message == (
        "invalid shared worker tools/call response from example-stateless"
    )
    assert exc_info.value.upstream_name == "example-stateless"
    assert exc_info.value.tool_name == "echo"


@pytest.mark.parametrize("missing_field", ["tenant_context", "team_id", "quota_snapshot"])
def test_quota_decision_requires_complete_shared_context(missing_field: str) -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.hybrid_router import _quota_decision

    arguments = {
        "tenant_context": TENANT_CONTEXT,
        "team_id": "team-a",
        "upstream_id": "example-stateless",
        "tool_name": "example.echo",
        "quota_snapshot": _quota_snapshot(tool_name="example.echo"),
    }
    arguments[missing_field] = None

    with pytest.raises(BrokerToolError) as error:
        _quota_decision(
            **arguments,
        )

    assert error.value.code == "shared_routing_context_missing"
    assert error.value.message == (
        "shared routing requires tenant_context, team_id, and quota_snapshot"
    )
    assert error.value.upstream_name == "example-stateless"
    assert error.value.tool_name == "example.echo"


def test_shared_worker_quota_validation_errors_are_reported_as_tool_errors() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(lambda **_kwargs: {}),
    )

    with pytest.raises(BrokerToolError) as error:
        router.call_tool(
            advertised_name="example.echo",
            arguments={},
            edge_caller=RecordingEdgeCaller().call,
            tenant_context={**TENANT_CONTEXT, "tenant_id": ""},
            team_id="team-a",
            quota_snapshot=_quota_snapshot(tool_name="example.echo"),
        )

    assert error.value.code == "shared_routing_quota_invalid"
    assert error.value.message == "tenant_id is required"
    assert error.value.upstream_name == "example-stateless"
    assert error.value.tool_name == "example.echo"


def test_hybrid_router_enforces_profile_before_routing() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute
    from mcp_broker.profiles import ToolExposureProfile

    router = HybridToolRouter(
        upstreams={
            "local-store": UpstreamConfig(
                name="local-store",
                command="local-store",
                tool_prefix="local-store",
                mode="per_session",
                tags=("stateful",),
                profiles=("allowed",),
            )
        },
        shared_worker=SharedWorkerRoute(lambda **_: {}),
        profile=ToolExposureProfile(name="blocked", max_tools=10),
    )

    with pytest.raises(BrokerToolError) as exc_info:
        router.call_tool(
            advertised_name="local-store.search",
            arguments={},
            edge_caller=RecordingEdgeCaller().call,
            tenant_context=TENANT_CONTEXT,
            team_id="team-a",
            quota_snapshot=_quota_snapshot(),
        )
    assert exc_info.value.code == "profile_denied"


def test_hybrid_router_reuses_supplied_call_locks_for_serialized_edge_calls() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    call_locks: dict[str, object] = {}
    router = HybridToolRouter(
        upstreams={
            "local-store": UpstreamConfig(
                name="local-store",
                command="local-store",
                tool_prefix="local-store",
                mode="per_session",
                tags=("stateful",),
                serialize_calls=True,
            )
        },
        shared_worker=SharedWorkerRoute(lambda **_: {}),
        call_locks=call_locks,
    )

    router.call_tool(
        advertised_name="local-store.search",
        arguments={},
        edge_caller=RecordingEdgeCaller().call,
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(),
    )

    assert set(call_locks) == {"local-store"}


@pytest.mark.parametrize("missing_field", ["tenant_context", "team_id", "quota_snapshot"])
def test_hybrid_router_uses_edge_when_shared_context_is_incomplete(
    missing_field: str,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    edge = RecordingEdgeCaller()
    worker = RecordingSharedWorker()
    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(worker.call_tool),
    )
    call_args = {
        "tenant_context": TENANT_CONTEXT,
        "team_id": "team-a",
        "quota_snapshot": _quota_snapshot(tool_name="example.echo"),
    }
    call_args[missing_field] = None

    result = router.call_tool(
        advertised_name="example.echo",
        arguments={},
        edge_caller=edge.call,
        **call_args,
    )

    assert result["structuredContent"] == {"execution_boundary": "local_edge"}
    assert worker.calls == []


@pytest.mark.parametrize(
    "local_state",
    [
        {"state_dir": "state"},
        {"env": {"EXAMPLE": "value"}},
        {"env_files": {"EXAMPLE": Path("example.env")}},
        {"session_env": {"SESSION": "session_id"}},
        {"request_meta": {"client": "client_name"}},
    ],
)
def test_requires_local_state_checks_each_supported_source(
    local_state: dict[str, object],
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import _requires_local_state

    upstream = UpstreamConfig(
        name="example-stateless",
        command="example-stateless",
        **local_state,
    )

    assert _requires_local_state(upstream) is True


def test_shared_worker_dict_response_still_requires_valid_mcp_content() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter, SharedWorkerRoute

    router = HybridToolRouter(
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
        shared_worker=SharedWorkerRoute(
            lambda **_: {"allowed": True, "result": {"content": "invalid"}}
        ),
    )

    with pytest.raises(BrokerToolError) as exc_info:
        router.call_tool(
            advertised_name="example.echo",
            arguments={},
            edge_caller=RecordingEdgeCaller().call,
            tenant_context=TENANT_CONTEXT,
            team_id="team-a",
            quota_snapshot=_quota_snapshot(tool_name="example.echo"),
        )
    assert exc_info.value.code == "invalid_upstream_response"
    assert exc_info.value.message == (
        "invalid upstream tools/call response from example-stateless"
    )
    assert exc_info.value.upstream_name == "example-stateless"


class RecordingEdgeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], int]] = []

    def call(
        self,
        upstream_name: str,
        upstream_tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append((upstream_name, upstream_tool_name, arguments, timeout_seconds))
        return {
            "content": [{"type": "text", "text": f"edge:{upstream_name}.{upstream_tool_name}"}],
            "structuredContent": {"execution_boundary": "local_edge"},
        }


class RecordingSharedWorker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def call_tool(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        quota_decision = kwargs["quota_decision"]
        if isinstance(quota_decision, dict) and quota_decision.get("allowed") is False:
            return {
                "allowed": False,
                "reason": "quota_denied",
                "result": None,
                "audit_event": {
                    "event_type": "shared_worker_call",
                    "tenant_id": "tenant-a",
                    "workspace_id": "workspace-a",
                    "user_id": "user-a",
                    "upstream_id": kwargs["upstream_id"],
                    "tool_name": kwargs["tool_name"],
                    "result": "denied",
                    "denial_reason": quota_decision["reason"],
                },
            }
        tool_name = kwargs["tool_name"]
        return {
            "allowed": True,
            "reason": "worker_call_allowed",
            "result": {
                "content": [{"type": "text", "text": f"worker:{tool_name}"}],
                "structuredContent": {
                    "execution_boundary": "shared_worker",
                    "arguments": kwargs["arguments"],
                },
            },
        }


def _quota_snapshot(
    *,
    tool_name: str = "local-store.search",
    tool_limit: int = 10,
    tool_used: int = 0,
) -> dict[str, object]:
    return {
        "kill_switches": {
            "global": False,
            "teams": [],
            "users": [],
            "upstreams": [],
            "tools": [],
        },
        "limits": {
            "global": {"limit": 100, "used": 0},
            "teams": {"team-a": {"limit": 100, "used": 0}},
            "users": {"user-a": {"limit": 100, "used": 0}},
            "upstreams": {"example-stateless": {"limit": 100, "used": 0}},
            "tools": {tool_name: {"limit": tool_limit, "used": tool_used}},
        },
    }
