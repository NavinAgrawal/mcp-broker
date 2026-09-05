from pathlib import Path
import pytest
from mcp_broker.catalog import (
    BrokerCatalogFacade,
    _looks_like_auth_error,
    _snapshot_int,
    _specific_query_can_select_upstream,
    catalog_entries_for_upstream,
    catalog_entry_matches,
    catalog_unavailable_entry_for_upstream,
    profile_allows_upstream,
    structured_tool_result,
    upstream_metadata_matches,
    upstream_owns_tool_name,
)
from mcp_broker.config import (
    BrokerConfig,
    BrokerSettings,
    RuntimeConfig,
    SmokeProbe,
    UpstreamConfig,
)
from mcp_broker.profiles import ToolExposureProfile
pytestmark = pytest.mark.unit

@pytest.mark.parametrize(
    "message",
    [
        "AUTH failed",
        "missing credential",
        "forbidden by provider",
        "bad token",
        "unauthorized request",
        "HTTP 401",
        "HTTP 403",
    ],
)
def test_auth_error_detection_matches_every_supported_marker(message: str) -> None:
    assert _looks_like_auth_error(message) is True

def test_auth_error_detection_rejects_non_authentication_errors() -> None:
    assert _looks_like_auth_error("missing DISPLAY") is False

def test_snapshot_int_defaults_to_zero_when_no_integer_value_exists() -> None:
    assert _snapshot_int({}, "primary", "legacy") == 0

def _projection_error_message(projection: object) -> str:
    from mcp_broker.catalog import apply_projection

    with pytest.raises(ValueError) as exc:
        apply_projection({"content": []}, projection)  # type: ignore[arg-type]
    return str(exc.value)
def _catalog_config(tmp_path: Path) -> BrokerConfig:
    return BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={
            "default-llm": ToolExposureProfile(
                name="default-llm",
                max_tools=20,
                compact_tools_enabled=True,
                allow_mutating_upstreams=("write-store",),
            ),
            "other-llm": ToolExposureProfile(name="other-llm", max_tools=20),
        },
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
                purpose="Read records",
                tags=("records",),
            ),
            "write-store": UpstreamConfig(
                name="write-store",
                command="write-store",
                tool_prefix="write",
                profiles=("default-llm",),
                mutating=True,
            ),
            "broken-store": UpstreamConfig(
                name="broken-store",
                command="broken-store",
                profiles=("default-llm",),
            ),
            "other-profile-store": UpstreamConfig(
                name="other-profile-store",
                command="other-profile-store",
                profiles=("other-llm",),
            ),
            "disabled-store": UpstreamConfig(
                name="disabled-store",
                command="disabled-store",
                enabled=False,
                profiles=("default-llm",),
            ),
        },
    )
def _runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        log_dir=tmp_path / "runtime" / "logs",
        state_dir=tmp_path / "runtime" / "state",
        secrets_dir=tmp_path / "runtime" / "secrets",
    )

@pytest.mark.parametrize(
    ("last_error", "expected_state"),
    [
        ("auth failed", "unauthenticated"),
        ("missing credential", "unauthenticated"),
        ("forbidden by provider", "unauthenticated"),
        ("bad token", "unauthenticated"),
        ("unauthorized request", "unauthenticated"),
        ("HTTP 401", "unauthenticated"),
        ("HTTP 403", "unauthenticated"),
        ("missing DISPLAY", "unknown"),
        (None, "unknown"),
    ],
)
def test_status_infers_auth_state_from_last_error(
    tmp_path: Path,
    last_error: str | None,
    expected_state: str,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {"read-store": {"last_error": last_error}},
    ).call_tool("broker.status", {})

    assert result["structuredContent"]["upstreams"]["read-store"]["auth_state"] == expected_state

def test_status_defaults_missing_auth_probe_to_none(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {"read-store": {}},
    ).call_tool("broker.status", {})

    assert result["structuredContent"]["upstreams"]["read-store"]["auth_probe"] == "none"

@pytest.mark.parametrize("auth_state", ["authenticated", "unauthenticated", "unknown"])
def test_status_preserves_explicit_auth_state_values(
    tmp_path: Path,
    auth_state: str,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {
            "read-store": {
                "auth_state": auth_state,
                "last_error": "display unavailable",
            }
        },
    ).call_tool("broker.status", {})

    assert result["structuredContent"]["upstreams"]["read-store"]["auth_state"] == auth_state

def test_status_preserves_explicit_unknown_auth_state_over_auth_looking_errors(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {
            "read-store": {
                "auth_state": "unknown",
                "last_error": "HTTP 403 forbidden",
            }
        },
    ).call_tool("broker.status", {})

    assert result["structuredContent"]["upstreams"]["read-store"]["auth_state"] == "unknown"
