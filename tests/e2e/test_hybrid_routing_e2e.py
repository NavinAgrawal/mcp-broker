import pytest


pytestmark = pytest.mark.e2e


TENANT_A = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}

TENANT_B = {
    "tenant_id": "tenant-b",
    "workspace_id": "workspace-b",
    "user_id": "user-b",
}


def test_hybrid_router_preserves_client_tool_names_across_edge_and_worker() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    edge_calls: list[tuple[str, str, dict[str, object], int]] = []

    def edge_call(
        upstream_name: str,
        upstream_tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        edge_calls.append((upstream_name, upstream_tool_name, arguments, timeout_seconds))
        return {
            "content": [{"type": "text", "text": "edge"}],
            "structuredContent": {
                "execution_boundary": "local_edge",
                "upstream_tool_name": upstream_tool_name,
            },
        }

    worker = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )
    router = HybridToolRouter(
        upstreams={
            "local-store": UpstreamConfig(
                name="local-store",
                command="local-store",
                tool_prefix="local-store",
                mode="per_session",
                tags=("stateful",),
                state_dir="local-store",
            ),
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            ),
        },
        shared_worker=worker,
    )

    edge = router.call_tool(
        advertised_name="local-store.search",
        arguments={"query": "refund"},
        edge_caller=edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="local-store",
            tool_name="local-store.search",
        ),
    )
    shared = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "hello"},
        edge_caller=edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="example-stateless",
            tool_name="example.echo",
        ),
    )

    assert edge["structuredContent"] == {
        "execution_boundary": "local_edge",
        "upstream_tool_name": "search",
    }
    assert shared == {
        "content": [{"type": "text", "text": "hello"}],
        "structuredContent": {"message": "hello"},
    }
    assert edge_calls == [("local-store", "search", {"query": "refund"}, 60)]


def test_hybrid_worker_routing_keeps_tenant_scopes_isolated() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    worker = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )
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
        shared_worker=worker,
    )

    first = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "tenant-a"},
        edge_caller=_unexpected_edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="example-stateless",
            tool_name="example.echo",
        ),
    )
    second = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "tenant-b"},
        edge_caller=_unexpected_edge_call,
        tenant_context=TENANT_B,
        team_id="team-b",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_B,
            team_id="team-b",
            upstream_id="example-stateless",
            tool_name="example.echo",
        ),
    )

    assert first["structuredContent"] == {"message": "tenant-a"}
    assert second["structuredContent"] == {"message": "tenant-b"}
    assert worker.state_snapshot() == {}


def test_hybrid_shared_worker_quota_denial_does_not_fallback_to_edge() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.hybrid_router import HybridToolRouter
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    worker = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )
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
        shared_worker=worker,
    )

    decision = router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "blocked"},
        edge_caller=_unexpected_edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="example-stateless",
            tool_name="example.echo",
            tool_limit=0,
            tool_used=0,
        ),
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "quota_denied"
    assert decision["audit_event"]["denial_reason"] == "tool_quota_exceeded"


def _unexpected_edge_call(*_args: object, **_kwargs: object) -> dict[str, object]:
    raise AssertionError("shared-worker route must not call the edge broker")


def _quota_snapshot(
    *,
    tenant: dict[str, str],
    team_id: str,
    upstream_id: str,
    tool_name: str,
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
            "teams": {team_id: {"limit": 100, "used": 0}},
            "users": {tenant["user_id"]: {"limit": 100, "used": 0}},
            "upstreams": {upstream_id: {"limit": 100, "used": 0}},
            "tools": {tool_name: {"limit": tool_limit, "used": tool_used}},
        },
    }
