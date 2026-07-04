import pytest


pytestmark = pytest.mark.unit


TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}


def _quota_snapshot() -> dict[str, object]:
    return {
        "kill_switches": {
            "global": False,
            "teams": [],
            "users": [],
            "upstreams": [],
            "tools": [],
        },
        "limits": {
            "global": {"limit": 100, "used": 10},
            "teams": {"team-a": {"limit": 50, "used": 20}},
            "users": {"user-a": {"limit": 25, "used": 5}},
            "upstreams": {"example-upstream": {"limit": 30, "used": 3}},
            "tools": {"example.search": {"limit": 15, "used": 2}},
        },
    }


def _decision(snapshot: dict[str, object]) -> dict[str, object]:
    from mcp_broker.quota_policy import decide_quota

    return decide_quota(
        tenant_context=TENANT_CONTEXT,
        team_id="team-a",
        upstream_id="example-upstream",
        tool_name="example.search",
        quota_snapshot=snapshot,
    )


def test_quota_policy_declares_default_deny_and_no_external_metering() -> None:
    from mcp_broker.quota_policy import build_quota_policy

    policy = build_quota_policy()

    assert policy["schema_version"] == 1
    assert policy["default_decision"] == "deny"
    assert policy["external_metering_supported"] is False
    assert policy["enforced_scopes"] == ["global", "team", "user", "upstream", "tool"]
    assert policy["denial_audit_required"] is True


def test_quota_decision_allows_when_every_scope_is_under_limit() -> None:
    decision = _decision(_quota_snapshot())

    assert decision["allowed"] is True
    assert decision["reason"] == "quota_allowed"
    assert decision["checked_scopes"] == ["global", "team", "user", "upstream", "tool"]
    assert decision["audit_event"]["result"] == "allowed"
    assert decision["audit_event"]["tenant_id"] == "tenant-a"
    assert decision["audit_event"]["tool_name"] == "example.search"


@pytest.mark.parametrize(
    ("switch_path", "expected_reason"),
    [
        (("global",), "global_kill_switch"),
        (("teams", "team-a"), "team_kill_switch"),
        (("users", "user-a"), "user_kill_switch"),
        (("upstreams", "example-upstream"), "upstream_kill_switch"),
        (("tools", "example.search"), "tool_kill_switch"),
    ],
)
def test_quota_decision_denies_when_any_kill_switch_matches(
    switch_path: tuple[str, ...],
    expected_reason: str,
) -> None:
    snapshot = _quota_snapshot()
    kill_switches = snapshot["kill_switches"]
    assert isinstance(kill_switches, dict)
    if len(switch_path) == 1:
        kill_switches[switch_path[0]] = True
    else:
        kill_switches[switch_path[0]] = [switch_path[1]]

    decision = _decision(snapshot)

    assert decision["allowed"] is False
    assert decision["reason"] == expected_reason
    assert decision["audit_event"]["result"] == "denied"
    assert decision["audit_event"]["denial_reason"] == expected_reason


@pytest.mark.parametrize(
    ("scope", "limit_path", "expected_reason"),
    [
        ("global", ("global",), "global_quota_exceeded"),
        ("team", ("teams", "team-a"), "team_quota_exceeded"),
        ("user", ("users", "user-a"), "user_quota_exceeded"),
        ("upstream", ("upstreams", "example-upstream"), "upstream_quota_exceeded"),
        ("tool", ("tools", "example.search"), "tool_quota_exceeded"),
    ],
)
def test_quota_decision_denies_when_any_scope_is_at_limit(
    scope: str,
    limit_path: tuple[str, ...],
    expected_reason: str,
) -> None:
    snapshot = _quota_snapshot()
    limits = snapshot["limits"]
    assert isinstance(limits, dict)
    if len(limit_path) == 1:
        limit_record = limits[limit_path[0]]
    else:
        nested_limits = limits[limit_path[0]]
        assert isinstance(nested_limits, dict)
        limit_record = nested_limits[limit_path[1]]
    assert isinstance(limit_record, dict)
    limit_record["used"] = limit_record["limit"]

    decision = _decision(snapshot)

    assert decision["allowed"] is False
    assert decision["reason"] == expected_reason
    assert decision["blocked_scope"] == scope
    assert decision["audit_event"]["denial_reason"] == expected_reason


def test_quota_decision_fails_closed_when_scope_limit_is_missing() -> None:
    snapshot = _quota_snapshot()
    limits = snapshot["limits"]
    assert isinstance(limits, dict)
    limits.pop("tools")

    decision = _decision(snapshot)

    assert decision["allowed"] is False
    assert decision["reason"] == "tool_quota_missing"
    assert decision["blocked_scope"] == "tool"
    assert decision["audit_event"]["denial_reason"] == "tool_quota_missing"


def test_quota_decision_rejects_missing_tenant_context() -> None:
    from mcp_broker.quota_policy import QuotaPolicyError, decide_quota

    with pytest.raises(QuotaPolicyError, match="tenant_id is required"):
        decide_quota(
            tenant_context={
                "workspace_id": "workspace-a",
                "user_id": "user-a",
            },
            team_id="team-a",
            upstream_id="example-upstream",
            tool_name="example.search",
            quota_snapshot=_quota_snapshot(),
        )


def test_quota_denial_audit_payload_contains_attribution_scope() -> None:
    snapshot = _quota_snapshot()
    snapshot["kill_switches"] = {
        "global": False,
        "teams": [],
        "users": ["user-a"],
        "upstreams": [],
        "tools": [],
    }

    decision = _decision(snapshot)

    assert decision["allowed"] is False
    assert decision["audit_event"] == {
        "event_type": "quota_decision",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "team_id": "team-a",
        "upstream_id": "example-upstream",
        "tool_name": "example.search",
        "result": "denied",
        "denial_reason": "user_kill_switch",
    }
