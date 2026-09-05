from pathlib import Path
import pytest
from mcp_broker.discovery_parity import DiscoveryParityError
from mcp_broker.profile_validation import (
    ProfileProbe,
    build_profile_validation_plan,
    run_profile_validation,
    _call_probe_if_enabled,
    _load_facade_state,
    _load_upstream_status,
    _require_exposed_upstream,
    _run_single_probe,
    _normalize_probe,
    _parse_args,
    _raise_on_unavailable_probe_catalog,
    _raise_on_unhealthy_exposed_upstreams,
    _search_and_describe_probe,
    _search_probe_payload,
    _upstreams_from_status,
)
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
class SequenceRequester:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses

    def __call__(self, *args, **kwargs) -> dict:
        return self.responses.pop(0)
class RecordingSequenceRequester(SequenceRequester):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        return super().__call__(*args, **kwargs)
def _plan_config(tmp_path: Path) -> dict:
    return {
        "runtime": {"root": str(tmp_path / "runtime")},
        "profiles": {
            "llm-profile": {"max_tools": 80, "compact_tools_enabled": True},
            "other-llm": {"max_tools": 80, "compact_tools_enabled": True},
        },
        "upstreams": {
            "callable": _upstream_config(["llm-profile", "other-llm"], "callable.status"),
            "missing": {"command": "missing", "enabled": True, "mode": "shared", "profiles": ["llm-profile"]},
            "missing-later": {
                "command": "missing-later",
                "enabled": True,
                "mode": "shared",
                "profiles": ["llm-profile"],
            },
            "other-profile": _upstream_config(["other-llm"], "other.status"),
            "disabled": {"command": "disabled", "enabled": False, "mode": "disabled", "profiles": ["llm-profile"]},
            "disabled-shared": {
                **_upstream_config(["llm-profile"], "disabled-shared.status"),
                "enabled": False,
                "mode": "shared",
            },
            "mode-disabled": {
                **_upstream_config(["llm-profile"], "mode-disabled.status"),
                "enabled": True,
                "mode": "disabled",
            },
        },
    }
def _upstream_config(profiles: list[str], tool: str) -> dict:
    return {
        "command": tool.split(".", 1)[0],
        "enabled": True,
        "mode": "shared",
        "profiles": profiles,
        "smoke": {"query": tool, "tool": tool, "arguments": {}},
    }
def _describe_response(tool_name: str, *, schema: dict | None = None) -> dict:
    input_schema = schema or {"type": "object", "additionalProperties": False}
    return {"result": {"structuredContent": {"tool": {"name": tool_name, "inputSchema": input_schema}}}}
def _success_responses() -> list[dict]:
    return [
        {"result": {}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "search-only": {"exposed": True, "state": "running"},
                    }
                }
            }
        },
        {"result": {"tools": [{"name": "broker.status"}, {"name": "broker.search_tools"}]}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "search-only": {"exposed": True, "state": "running"},
                    }
                }
            }
        },
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        _describe_response("callable.status"),
        {"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "search-only": {"exposed": True, "state": "ready"},
                    }
                }
            }
        },
        {"result": {"structuredContent": {"matches": [{"name": "search-only.lookup"}]}}},
        _describe_response("search-only.lookup"),
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "search-only": {"exposed": True, "state": "running"},
                    }
                }
            }
        },
    ]

def test_call_probe_if_enabled_rejects_upstream_error_flag() -> None:
    requester = RecordingSequenceRequester(
        [{"result": {"isError": True, "content": [{"type": "text", "text": "failed"}]}}]
    )

    with pytest.raises(DiscoveryParityError, match="probe returned upstream error: failed"):
        _call_probe_if_enabled(
            socket_path=Path("/tmp/profile-validation.sock"),
            profile="llm-profile",
            session_id="session",
            probe=ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
            ),
            input_schema={"type": "object", "additionalProperties": False},
            request_fn=requester,
        )

def test_call_probe_if_enabled_rejects_error_text_prefix() -> None:
    requester = RecordingSequenceRequester(
        [{"result": {"content": [{"type": "text", "text": "Error: unavailable"}]}}]
    )

    with pytest.raises(DiscoveryParityError, match="probe returned upstream error: Error: unavailable"):
        _call_probe_if_enabled(
            socket_path=Path("/tmp/profile-validation.sock"),
            profile="llm-profile",
            session_id="session",
            probe=ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
            ),
            input_schema={"type": "object", "additionalProperties": False},
            request_fn=requester,
        )

def test_run_profile_validation_rejects_invalid_status_payload() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": []}}},
    ]
    with pytest.raises(DiscoveryParityError, match="broker.status returned invalid upstream map"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_when_status_does_not_expose_upstream() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": False, "state": "configured"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": False}}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="broker.status did not expose callable"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_when_exposed_upstream_is_not_healthy() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "exited"}}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="callable is not healthy"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

@pytest.mark.parametrize("bad_state", ["exited", "failed", "backoff"])
def test_require_exposed_upstream_rejects_terminal_states(bad_state: str) -> None:
    with pytest.raises(DiscoveryParityError) as exc_info:
        _require_exposed_upstream(
            "llm-profile",
            {"callable": {"exposed": True, "state": bad_state}},
            "callable",
            context="",
            require_active=True,
        )
    assert str(exc_info.value) == f"llm-profile upstream callable is not healthy: state='{bad_state}'"

@pytest.mark.parametrize("bad_state", ["exited", "failed", "backoff"])
def test_require_exposed_upstream_rejects_terminal_states_before_probe(bad_state: str) -> None:
    with pytest.raises(
        DiscoveryParityError,
        match=f"llm-profile upstream callable is not healthy: state='{bad_state}'",
    ):
        _require_exposed_upstream(
            "llm-profile",
            {"callable": {"exposed": True, "state": bad_state}},
            "callable",
            context="",
            require_active=False,
        )

def test_require_exposed_upstream_allows_configured_state_before_probe_only() -> None:
    snapshot = {"exposed": True, "state": "configured"}

    assert (
        _require_exposed_upstream(
            "llm-profile",
            {"callable": snapshot},
            "callable",
            context="",
            require_active=False,
        )
        is snapshot
    )
    with pytest.raises(DiscoveryParityError, match="state='configured'"):
        _require_exposed_upstream(
            "llm-profile",
            {"callable": snapshot},
            "callable",
            context="",
            require_active=True,
        )

def test_require_exposed_upstream_default_requires_active_state() -> None:
    with pytest.raises(DiscoveryParityError) as exc_info:
        _require_exposed_upstream(
            "llm-profile",
            {"callable": {"exposed": True, "state": "configured"}},
            "callable",
            context="",
            require_active=True,
        )

    assert str(exc_info.value) == "llm-profile upstream callable is not healthy: state='configured'"

def test_require_exposed_upstream_rejects_last_error_with_context() -> None:
    with pytest.raises(DiscoveryParityError, match="after probe: last_error='boom'"):
        _require_exposed_upstream(
            "llm-profile",
            {"callable": {"exposed": True, "state": "running", "last_error": "boom"}},
            "callable",
            context="after probe",
            require_active=True,
        )

def test_require_exposed_upstream_rejects_last_error_without_context() -> None:
    with pytest.raises(
        DiscoveryParityError,
        match="llm-profile upstream callable is not healthy: last_error='boom'",
    ):
        _require_exposed_upstream(
            "llm-profile",
            {"callable": {"exposed": True, "state": "running", "last_error": "boom"}},
            "callable",
            context="",
            require_active=True,
        )

def test_raise_on_unhealthy_exposed_upstreams_ignores_hidden_upstreams() -> None:
    _raise_on_unhealthy_exposed_upstreams(
        "llm-profile",
        {
            "hidden": {"exposed": False, "state": "failed", "last_error": "hidden"},
            "malformed": "not a snapshot",
        },
    )

def test_raise_on_unhealthy_exposed_upstreams_keeps_scanning_after_hidden_upstream() -> None:
    with pytest.raises(DiscoveryParityError, match="state='failed'"):
        _raise_on_unhealthy_exposed_upstreams(
            "llm-profile",
            {
                "hidden": {"exposed": False, "state": "running"},
                "callable": {"exposed": True, "state": "failed"},
            },
        )

def test_raise_on_unhealthy_exposed_upstreams_rejects_last_error() -> None:
    with pytest.raises(DiscoveryParityError, match="state='running'"):
        _raise_on_unhealthy_exposed_upstreams(
            "llm-profile",
            {"callable": {"exposed": True, "state": "running", "last_error": "boom"}},
        )

@pytest.mark.parametrize("bad_state", ["exited", "failed", "backoff"])
def test_raise_on_unhealthy_exposed_upstreams_rejects_terminal_state(bad_state: str) -> None:
    with pytest.raises(
        DiscoveryParityError,
        match=f"llm-profile upstream callable is not healthy before probe: state='{bad_state}'",
    ):
        _raise_on_unhealthy_exposed_upstreams(
            "llm-profile",
            {"callable": {"exposed": True, "state": bad_state}},
        )

def test_run_profile_validation_fails_when_probe_leaves_upstream_unhealthy() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        _describe_response("callable.status"),
        {"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "exited"}}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="callable is not healthy after probe"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_when_status_reports_last_error_before_search() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {
                            "exposed": True,
                            "state": "running",
                            "last_error": "subprocess crashed",
                        }
                    }
                }
            }
        },
    ]
    with pytest.raises(DiscoveryParityError, match="last_error='subprocess crashed'"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_upstreams_from_status_treats_missing_upstream_map_as_empty() -> None:
    assert _upstreams_from_status("llm-profile", {"result": {"structuredContent": {}}}) == {}

def test_load_upstream_status_uses_profile_in_invalid_status_error() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile broker.status returned invalid upstream map"):
        _load_upstream_status(
            Path("/tmp/profile-validation.sock"),
            "llm-profile",
            "session",
            SequenceRequester([{"result": {"structuredContent": {"upstreams": []}}}]),
        )

def test_load_facade_state_uses_profile_for_initial_and_final_status_errors() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile broker.status returned invalid upstream map"):
        _load_facade_state(
            Path("/tmp/profile-validation.sock"),
            "llm-profile",
            "session",
            SequenceRequester(
                [
                    {"result": {}},
                    {"result": {"structuredContent": {"upstreams": []}}},
                ]
            ),
        )
    with pytest.raises(DiscoveryParityError, match="llm-profile broker.status returned invalid upstream map"):
        _load_facade_state(
            Path("/tmp/profile-validation.sock"),
            "llm-profile",
            "session",
            SequenceRequester(
                [
                    {"result": {}},
                    {"result": {"structuredContent": {"upstreams": {}}}},
                    {"result": {"tools": []}},
                    {"result": {"structuredContent": {"upstreams": []}}},
                ]
            ),
        )

def test_load_facade_state_uses_profile_for_initial_unhealthy_error() -> None:
    with pytest.raises(
        DiscoveryParityError,
        match="llm-profile upstream callable is not healthy before probe: state='failed'",
    ):
        _load_facade_state(
            Path("/tmp/profile-validation.sock"),
            "llm-profile",
            "session",
            SequenceRequester(
                [
                    {"result": {}},
                    {
                        "result": {
                            "structuredContent": {
                                "upstreams": {"callable": {"exposed": True, "state": "failed"}}
                            }
                        }
                    },
                ]
            ),
        )

def test_run_profile_validation_fails_when_search_skips_probe_upstream() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {
            "result": {
                "structuredContent": {
                    "matches": [{"name": "callable", "upstream": "callable", "available": False}],
                    "skipped_upstreams": {"callable": "process exited"},
                }
            }
        },
    ]
    with pytest.raises(DiscoveryParityError, match="search skipped callable"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_when_search_matches_are_not_a_list() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": {"name": "callable.status"}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="search returned invalid matches"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_when_search_marks_probe_upstream_unavailable() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {
            "result": {
                "structuredContent": {
                    "matches": [{"name": "callable", "upstream": "callable", "available": False}],
                }
            }
        },
    ]
    with pytest.raises(DiscoveryParityError, match="search marked callable unavailable"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_raise_on_unavailable_probe_catalog_ignores_other_upstream_unavailable_match() -> None:
    _raise_on_unavailable_probe_catalog(
        "llm-profile",
        {
            "matches": [
                {"name": "other.status", "upstream": "other", "available": False},
                {"name": "callable.status", "upstream": "callable", "available": True},
            ]
        },
        ProfileProbe(
            upstream_name="callable",
            query="callable status",
            tool="callable.status",
            arguments={},
        ),
    )

def test_raise_on_unavailable_probe_catalog_reports_matching_unavailable_tool() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile search marked callable unavailable"):
        _raise_on_unavailable_probe_catalog(
            "llm-profile",
            {"matches": [{"name": "callable.status", "upstream": "callable", "available": False}]},
            ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
            ),
        )

def test_raise_on_unavailable_probe_catalog_rejects_malformed_skipped_upstreams() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile search returned invalid skipped_upstreams"):
        _raise_on_unavailable_probe_catalog(
            "llm-profile",
            {"matches": [], "skipped_upstreams": ["callable"]},
            ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
            ),
        )

def test_search_and_describe_probe_reports_profile_when_catalog_skips_upstream() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile search skipped callable: process exited"):
        _search_and_describe_probe(
            socket_path=Path("/tmp/profile-validation.sock"),
            profile="llm-profile",
            session_id="session",
            probe=ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
            ),
            request_fn=SequenceRequester(
                [
                    {
                        "result": {
                            "structuredContent": {
                                "matches": [],
                                "skipped_upstreams": {"callable": "process exited"},
                            }
                        }
                    }
                ]
            ),
        )
