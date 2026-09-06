import pytest


pytestmark = pytest.mark.unit


def test_schema_parse_profiles_defaults_and_preserves_values() -> None:
    from mcp_broker.schema import parse_profiles

    assert parse_profiles("upstreams.docs.profiles", None) == ("manual-test",)
    assert parse_profiles("upstreams.docs.profiles", ["codex", "review"]) == (
        "codex",
        "review",
    )


@pytest.mark.parametrize("value", [[], "", [""]])
def test_schema_parse_profiles_rejects_empty_values(value: object) -> None:
    from mcp_broker.schema import parse_profiles

    with pytest.raises(ValueError, match="upstreams.docs.profiles"):
        parse_profiles("upstreams.docs.profiles", value)


def test_schema_parse_transport_defaults_and_rejects_empty_with_allowed_values() -> None:
    from mcp_broker.schema import parse_transport

    assert parse_transport("upstreams.docs.transport", None) == "stdio"
    assert parse_transport("upstreams.docs.transport", "http") == "http"

    with pytest.raises(
        ValueError,
        match="upstreams.docs.transport must be one of: http, sse, stdio",
    ):
        parse_transport("upstreams.docs.transport", "")


def test_schema_parse_mode_defaults_and_rejects_empty_with_allowed_values() -> None:
    from mcp_broker.schema import parse_mode

    assert parse_mode("upstreams.docs.mode", None) == "shared"
    assert parse_mode("upstreams.docs.mode", "per_session") == "per_session"
    assert parse_mode("upstreams.docs.mode", "per_call") == "per_call"

    with pytest.raises(
        ValueError,
        match="upstreams.docs.mode must be one of: disabled, per_call, per_session, shared",
    ):
        parse_mode("upstreams.docs.mode", "")


def test_schema_parse_startup_timeout_uses_default_only_for_none() -> None:
    from mcp_broker.schema import parse_startup_timeout

    assert parse_startup_timeout("upstreams.docs.startup_timeout_seconds", None) == 60
    assert parse_startup_timeout("upstreams.docs.startup_timeout_seconds", 1) == 1

    with pytest.raises(
        ValueError,
        match="upstreams.docs.startup_timeout_seconds must be greater than 0",
    ):
        parse_startup_timeout("upstreams.docs.startup_timeout_seconds", 0)


def test_schema_resource_policy_accepts_cpu_watchdog_bounds() -> None:
    from mcp_broker.schema import ResourcePolicy

    assert (
        ResourcePolicy.from_mapping(
            "upstreams.docs.resources",
            {"cpu_watchdog_percent": 1},
        ).cpu_watchdog_percent
        == 1
    )
    assert (
        ResourcePolicy.from_mapping(
            "upstreams.docs.resources",
            {"cpu_watchdog_percent": 100},
        ).cpu_watchdog_percent
        == 100
    )


@pytest.mark.parametrize("value", [0, 301])
def test_schema_health_policy_rejects_call_timeout_outside_bounds(value: int) -> None:
    from mcp_broker.schema import HealthPolicy

    with pytest.raises(
        ValueError,
        match="upstreams.docs.health.call_timeout_seconds must be between 1 and 300",
    ):
        HealthPolicy.from_mapping(
            "upstreams.docs.health",
            {"call_timeout_seconds": value},
        )


@pytest.mark.parametrize("value", [True, 1.5, "60"])
def test_schema_health_policy_rejects_non_integer_call_timeout(value: object) -> None:
    from mcp_broker.schema import HealthPolicy

    with pytest.raises(
        ValueError,
        match="upstreams.docs.health.call_timeout_seconds must be between 1 and 300",
    ):
        HealthPolicy.from_mapping(
            "upstreams.docs.health",
            {"call_timeout_seconds": value},
        )


@pytest.mark.parametrize("value", [0, 101])
def test_schema_resource_policy_rejects_cpu_watchdog_outside_bounds(value: int) -> None:
    from mcp_broker.schema import ResourcePolicy

    with pytest.raises(
        ValueError,
        match="upstreams.docs.resources.cpu_watchdog_percent must be between 1 and 100",
    ):
        ResourcePolicy.from_mapping(
            "upstreams.docs.resources",
            {"cpu_watchdog_percent": value},
        )
