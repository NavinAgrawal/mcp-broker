import pytest


pytestmark = pytest.mark.unit


TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}


def test_session_affinity_policy_declares_default_local_boundary() -> None:
    from mcp_broker.session_affinity import build_session_affinity_policy

    policy = build_session_affinity_policy()

    assert policy["schema_version"] == 1
    assert policy["default_execution_boundary"] == "local_edge"
    assert policy["local_only_upstream_classes"] == [
        "browser",
        "file_access",
        "local_secret",
        "oauth",
        "stateful",
        "unknown",
    ]
    assert policy["shared_eligible_upstream_classes"] == ["stateless"]
    assert policy["forbidden_upstream_classes"] == ["private_inventory"]


@pytest.mark.parametrize(
    "upstream_class",
    ["stateful", "oauth", "browser", "file_access", "local_secret", "unknown"],
)
def test_local_only_upstream_classes_bind_state_to_local_session(
    upstream_class: str,
) -> None:
    from mcp_broker.session_affinity import decide_session_affinity

    decision = decide_session_affinity(
        upstream_class=upstream_class,
        upstream_id="example-upstream",
        allowlisted=False,
        requires_local_state=True,
        tenant_context=TENANT_CONTEXT,
    )

    assert decision["execution_boundary"] == "local_edge"
    assert decision["session_affinity"] == "local_client_session"
    assert decision["state_binding"] == "local_edge_session"
    assert decision["shared_worker_eligible"] is False
    assert set(decision) == {
        "upstream_class",
        "execution_boundary",
        "session_affinity",
        "state_binding",
        "shared_worker_eligible",
        "reason",
        "state_scope",
    }
    assert decision["state_scope"] == {
        "session": "local_client_session",
        "upstream_id": "example-upstream",
    }


def test_allowlisted_stateless_without_local_state_can_bind_to_shared_worker() -> None:
    from mcp_broker.session_affinity import decide_session_affinity

    decision = decide_session_affinity(
        upstream_class="stateless",
        upstream_id="example-search",
        allowlisted=True,
        requires_local_state=False,
        tenant_context=TENANT_CONTEXT,
    )

    assert decision == {
        "upstream_class": "stateless",
        "execution_boundary": "shared_worker",
        "session_affinity": "tenant_workspace_user",
        "state_binding": "shared_worker_scope",
        "shared_worker_eligible": True,
        "reason": "allowlisted_stateless_upstream",
        "state_scope": {
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "upstream_id": "example-search",
        },
    }


@pytest.mark.parametrize(
    ("allowlisted", "requires_local_state"),
    [(False, False), (True, True), (False, True)],
)
def test_stateless_upstream_stays_local_without_both_shared_requirements(
    allowlisted: bool,
    requires_local_state: bool,
) -> None:
    from mcp_broker.session_affinity import decide_session_affinity

    decision = decide_session_affinity(
        upstream_class="stateless",
        upstream_id="example-search",
        allowlisted=allowlisted,
        requires_local_state=requires_local_state,
        tenant_context=TENANT_CONTEXT,
    )

    assert decision["execution_boundary"] == "local_edge"
    assert decision["session_affinity"] == "local_client_session"
    assert decision["state_binding"] == "local_edge_session"
    assert decision["shared_worker_eligible"] is False


def test_forbidden_private_inventory_class_fails_closed() -> None:
    from mcp_broker.session_affinity import (
        SessionAffinityError,
        decide_session_affinity,
    )

    with pytest.raises(
        SessionAffinityError,
        match=r"^upstream class is forbidden$",
    ):
        decide_session_affinity(
            upstream_class="private_inventory",
            upstream_id="example-upstream",
            allowlisted=False,
            requires_local_state=True,
            tenant_context=TENANT_CONTEXT,
        )


def test_shared_worker_state_binding_rejects_missing_tenant_context() -> None:
    from mcp_broker.session_affinity import (
        SessionAffinityError,
        decide_session_affinity,
    )

    with pytest.raises(SessionAffinityError, match="tenant_id is required"):
        decide_session_affinity(
            upstream_class="stateless",
            upstream_id="example-search",
            allowlisted=True,
            requires_local_state=False,
            tenant_context={
                "workspace_id": "workspace-a",
                "user_id": "user-a",
            },
        )


def test_blank_upstream_class_normalizes_to_unknown_local_boundary() -> None:
    from mcp_broker.session_affinity import decide_session_affinity

    decision = decide_session_affinity(
        upstream_class=" ",
        upstream_id="example-upstream",
        allowlisted=True,
        requires_local_state=False,
        tenant_context=TENANT_CONTEXT,
    )

    assert decision["upstream_class"] == "unknown"
    assert decision["execution_boundary"] == "local_edge"
    assert decision["shared_worker_eligible"] is False


def test_unrecognized_upstream_class_normalizes_to_unknown_local_boundary() -> None:
    from mcp_broker.session_affinity import decide_session_affinity

    decision = decide_session_affinity(
        upstream_class="custom-class",
        upstream_id="example-upstream",
        allowlisted=True,
        requires_local_state=False,
        tenant_context=TENANT_CONTEXT,
    )

    assert decision["upstream_class"] == "unknown"
    assert decision["execution_boundary"] == "local_edge"


@pytest.mark.parametrize("upstream_id", ["", " ", "tenant/upstream", r"tenant\\upstream"])
def test_session_affinity_rejects_invalid_upstream_id(upstream_id: str) -> None:
    from mcp_broker.session_affinity import (
        SessionAffinityError,
        decide_session_affinity,
    )

    with pytest.raises(
        SessionAffinityError,
        match=r"^upstream_id (?:is required|must not contain path separators)$",
    ):
        decide_session_affinity(
            upstream_class="stateful",
            upstream_id=upstream_id,
            allowlisted=False,
            requires_local_state=True,
            tenant_context=TENANT_CONTEXT,
        )


def test_shared_worker_state_binding_rejects_tenant_context_path_separator() -> None:
    from mcp_broker.session_affinity import (
        SessionAffinityError,
        decide_session_affinity,
    )

    with pytest.raises(SessionAffinityError, match="path separators"):
        decide_session_affinity(
            upstream_class="stateless",
            upstream_id="example-search",
            allowlisted=True,
            requires_local_state=False,
            tenant_context={
                "tenant_id": "tenant/a",
                "workspace_id": "workspace-a",
                "user_id": "user-a",
            },
        )
