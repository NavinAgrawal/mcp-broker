import json
from pathlib import Path
import pytest
from mcp_broker.discovery_parity import (
    DiscoveryParityError,
    _client_request,
    _parse_args,
    _profile_discovery_report,
    _profile_discovery_responses,
    _run_parity,
    _status_payload,
    _tool_payload,
    build_parity_report,
    compare_profile_discovery,
    run_profile_discovery,
)
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
class SequenceRequester:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses

    def __call__(self, *args, **kwargs) -> dict:
        return self.responses.pop(0)
class RecordingRequester:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"result": {}}
        self.calls: list[tuple[Path, str, str, dict]] = []

    def __call__(
        self,
        socket_path: Path,
        profile: str,
        session_id: str,
        payload: dict,
    ) -> dict:
        self.calls.append((socket_path, profile, session_id, payload))
        return self.response
def _profile_probe_responses() -> list[dict]:
    return [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.status"}, {"name": "broker.search_tools"}]}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {"hidden": {"exposed": False}, "read-store": {"exposed": True}}
                }
            }
        },
        {
            "result": {
                "structuredContent": {
                    "matches": [
                        {"name": "read-store.search"},
                        {"name": "read-store.get_project_scope"},
                    ]
                }
            }
        },
        {"result": {"structuredContent": {"tool": {"name": "read-store.get_project_scope"}}}},
        {"result": {"content": [{"type": "text", "text": '{"project": "demo"}'}]}},
    ]

def test_compare_profile_discovery_accepts_matching_profiles() -> None:
    left = {
        "profile": "codex",
        "advertised_tools": [
            "broker.call_tool",
            "broker.describe_tool",
            "broker.search_tools",
            "broker.status",
        ],
        "visible_upstreams": ["read-store"],
        "search_matches": ["read-store.get_project_scope"],
        "described_tool": "read-store.get_project_scope",
        "call_text": '{"project": "demo"}',
    }
    right = left | {"profile": "claude"}

    assert compare_profile_discovery(left, right) == []

def test_compare_profile_discovery_reports_all_profile_drift() -> None:
    left = {
        "profile": "codex",
        "advertised_tools": ["broker.search_tools"],
        "visible_upstreams": ["read-store"],
        "search_matches": ["read-store.get_project_scope"],
        "described_tool": "read-store.get_project_scope",
        "call_text": '{"project": "demo"}',
    }
    right = {
        "profile": "claude",
        "advertised_tools": ["broker.search_tools", "broker.status"],
        "visible_upstreams": ["read-store", "knowledge-service"],
        "search_matches": ["knowledge.kb_health"],
        "described_tool": "knowledge.kb_health",
        "call_text": '{"status": "ok"}',
    }

    assert compare_profile_discovery(left, right) == [
        "advertised_tools mismatch: codex=['broker.search_tools'] claude=['broker.search_tools', 'broker.status']",
        "visible_upstreams mismatch: codex=['read-store'] claude=['knowledge-service', 'read-store']",
        "search_matches mismatch: codex=['read-store.get_project_scope'] claude=['knowledge.kb_health']",
        "described_tool mismatch: codex='read-store.get_project_scope' claude='knowledge.kb_health'",
        "call_text mismatch: codex='{\"project\": \"demo\"}' claude='{\"status\": \"ok\"}'",
    ]

def test_compare_profile_discovery_checks_fields_after_matching_field() -> None:
    left = {
        "profile": "codex",
        "advertised_tools": ["broker.search_tools"],
        "visible_upstreams": ["read-store"],
        "search_matches": ["read-store.get_project_scope"],
        "described_tool": "read-store.get_project_scope",
        "call_text": '{"project": "demo"}',
    }
    right = left | {
        "profile": "claude",
        "visible_upstreams": ["write-store"],
        "call_text": '{"project": "other"}',
    }

    assert compare_profile_discovery(left, right) == [
        "visible_upstreams mismatch: codex=['read-store'] claude=['write-store']",
        "call_text mismatch: codex='{\"project\": \"demo\"}' claude='{\"project\": \"other\"}'",
    ]

def test_build_parity_report_indexes_profiles_and_match_state() -> None:
    left = {
        "profile": "codex",
        "advertised_tools": ["broker.search_tools"],
        "visible_upstreams": ["read-store"],
        "search_matches": ["read-store.get_project_scope"],
        "described_tool": "read-store.get_project_scope",
        "call_text": "{}",
    }
    right = left | {"profile": "claude"}

    assert build_parity_report(left=left, right=right, started_daemon=False) == {
        "matches": True,
        "mismatches": [],
        "profiles": {
            "codex": left,
            "claude": right,
        },
        "started_daemon": False,
    }

def test_profile_discovery_report_filters_and_sorts_discovery_payloads() -> None:
    assert _profile_discovery_report(
        profile="left-client",
        tools=[{"name": "broker.status"}, {"name": 42}, {"name": "broker.search_tools"}],
        upstreams={
            "write-service": {"exposed": "true"},
            "read-service": {"exposed": True},
            "hidden-service": {"exposed": False},
            "bad-service": [],
        },
        search_payload={
            "matches": [
                {"name": "read-service.lookup"},
                {"name": "read-service.health"},
            ]
        },
        describe_payload={"tool": {"name": "read-service.lookup"}},
        call_text='{"ok": true}',
    ) == {
        "profile": "left-client",
        "advertised_tools": ["42", "broker.search_tools", "broker.status"],
        "visible_upstreams": ["read-service"],
        "search_matches": ["read-service.health", "read-service.lookup"],
        "described_tool": "read-service.lookup",
        "call_text": '{"ok": true}',
    }

def test_status_payload_is_a_compact_broker_tool_call() -> None:
    assert _status_payload() == {
        "jsonrpc": "2.0",
        "id": "broker.status",
        "method": "tools/call",
        "params": {"name": "broker.status", "arguments": {}},
    }

def test_tool_payload_accepts_text_fallback() -> None:
    assert _tool_payload(
        {
            "result": {
                "content": [{"type": "text", "text": json.dumps({"matches": []})}],
            }
        }
    ) == {"matches": []}

def test_tool_payload_prefers_structured_content_object() -> None:
    assert _tool_payload(
        {
            "result": {
                "structuredContent": {"matches": [{"name": "read-service.lookup"}]},
                "content": [{"type": "text", "text": "not json"}],
            }
        }
    ) == {"matches": [{"name": "read-service.lookup"}]}

def test_tool_payload_rejects_non_object_text_payload() -> None:
    with pytest.raises(DiscoveryParityError) as exc_info:
        _tool_payload({"result": {"content": [{"type": "text", "text": "[]"}]}})
    assert str(exc_info.value) == "tool response payload must be object"

def test_client_request_delegates_to_client_shim() -> None:
    seen = {}

    def local_transport(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"result": {"ok": True}}

    assert _client_request(
        Path("/tmp/broker.sock"),
        "codex",
        "session",
        {"id": "one", "method": "tools/list"},
        transport_fn=local_transport,
    ) == {"result": {"ok": True}}
    assert seen == {
        "socket_path": Path("/tmp/broker.sock"),
        "profile": "codex",
        "session_id": "session",
        "payload": {"id": "one", "method": "tools/list"},
    }

def test_profile_discovery_responses_sends_expected_probe_sequence() -> None:
    socket_path = Path("/tmp/public-broker.sock")
    requester = RecordingRequester({"result": {"ok": True}})

    responses = _profile_discovery_responses(
        socket_path,
        "left-client",
        "session-1",
        "read",
        "read-service.lookup",
        {"limit": 1},
        requester,
    )

    assert list(responses) == ["initialize", "list", "status", "search", "describe", "call"]
    assert [call[:3] for call in requester.calls] == [
        (socket_path, "left-client", "session-1"),
        (socket_path, "left-client", "session-1"),
        (socket_path, "left-client", "session-1"),
        (socket_path, "left-client", "session-1"),
        (socket_path, "left-client", "session-1"),
        (socket_path, "left-client", "session-1"),
    ]
    payloads = [call[3] for call in requester.calls]
    assert [payload["method"] for payload in payloads] == [
        "initialize",
        "tools/list",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
    ]
    assert payloads[2] == _status_payload()
    assert payloads[3]["params"]["name"] == "broker.search_tools"
    assert payloads[3]["params"]["arguments"] == {"query": "read", "limit": 10}
    assert payloads[4]["params"]["name"] == "broker.describe_tool"
    assert payloads[4]["params"]["arguments"] == {"name": "read-service.lookup"}
    assert payloads[5]["params"]["name"] == "broker.call_tool"
    assert payloads[5]["params"]["arguments"] == {
        "name": "read-service.lookup",
        "arguments": {"limit": 1},
    }

def test_run_profile_discovery_rejects_tool_level_error() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {}}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": [{"type": "text", "text": "Error: missing"}]}},
    ]

    with pytest.raises(DiscoveryParityError, match="safe call returned upstream error"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_rejects_jsonrpc_tool_error_flag() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {}}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"isError": True, "content": [{"type": "text", "text": "upstream denied"}]}},
    ]

    with pytest.raises(DiscoveryParityError, match="safe call returned upstream error"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_rejects_missing_safe_call_content() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {}}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {}},
    ]

    with pytest.raises(DiscoveryParityError, match="safe call returned no text content"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_rejects_non_list_safe_call_content() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {}}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": {"text": "ok"}}},
    ]

    with pytest.raises(DiscoveryParityError, match="safe call returned no text content"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

@pytest.mark.parametrize(
    "content",
    [
        ["not-an-object"],
        [{"type": "text", "text": 42}],
    ],
)
def test_run_profile_discovery_rejects_malformed_safe_call_content(content: list[object]) -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {}}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": content}},
    ]

    with pytest.raises(DiscoveryParityError, match="safe call returned no text content"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_rejects_non_object_tool_payload() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"content": [{"type": "text", "text": "[]"}]}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": [{"type": "text", "text": "ok"}]}},
    ]

    with pytest.raises(DiscoveryParityError, match="tool response payload must be object"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="left-client",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_summarizes_profile_probe() -> None:
    assert run_profile_discovery(
        socket_path=Path("/tmp/unused.sock"),
        profile="codex",
        query="read-store",
        call_tool="read-store.get_project_scope",
        call_args={},
        session_id="session",
        request_fn=SequenceRequester(_profile_probe_responses()),
    ) == {
        "profile": "codex",
        "advertised_tools": ["broker.search_tools", "broker.status"],
        "visible_upstreams": ["read-store"],
        "search_matches": ["read-store.get_project_scope", "read-store.search"],
        "described_tool": "read-store.get_project_scope",
        "call_text": '{"project": "demo"}',
    }

def test_run_profile_discovery_passes_probe_arguments_to_response_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    seen: dict[str, object] = {}

    def fake_responses(
        socket_path: Path,
        profile: str,
        session_id: str,
        query: str,
        call_tool: str,
        call_args: dict,
        request_fn: object,
    ) -> dict[str, dict]:
        seen.update(
            {
                "socket_path": socket_path,
                "profile": profile,
                "session_id": session_id,
                "query": query,
                "call_tool": call_tool,
                "call_args": call_args,
                "request_fn": request_fn,
            }
        )
        return {
            "initialize": {"result": {}},
            "list": {"result": {"tools": [{"name": "broker.status"}]}},
            "status": {"result": {"structuredContent": {"upstreams": {}}}},
            "search": {"result": {"structuredContent": {"matches": []}}},
            "describe": {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
            "call": {"result": {"content": [{"type": "text", "text": "ok"}]}},
        }

    requester = RecordingRequester()
    monkeypatch.setattr(discovery_parity, "_profile_discovery_responses", fake_responses)

    run_profile_discovery(
        socket_path=Path("/tmp/broker.sock"),
        profile="left-client",
        query="echo",
        call_tool="echo.echo",
        call_args={"limit": 1},
        session_id="session-1",
        request_fn=requester,
    )

    assert seen == {
        "socket_path": Path("/tmp/broker.sock"),
        "profile": "left-client",
        "session_id": "session-1",
        "query": "echo",
        "call_tool": "echo.echo",
        "call_args": {"limit": 1},
        "request_fn": requester,
    }

def test_run_profile_discovery_rejects_invalid_status_payload() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": []}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": [{"type": "text", "text": "ok"}]}},
    ]

    with pytest.raises(DiscoveryParityError, match="broker.status returned invalid upstream map"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_discovery_rejects_missing_status_upstreams() -> None:
    responses = [
        {"result": {}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {}}},
        {"result": {"structuredContent": {"matches": []}}},
        {"result": {"structuredContent": {"tool": {"name": "echo.echo"}}}},
        {"result": {"content": [{"type": "text", "text": "ok"}]}},
    ]

    with pytest.raises(DiscoveryParityError, match="broker.status returned invalid upstream map"):
        run_profile_discovery(
            socket_path=Path("/tmp/unused.sock"),
            profile="codex",
            query="echo",
            call_tool="echo.echo",
            call_args={},
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_parse_args_uses_expected_defaults_and_overrides() -> None:
    args = _parse_args(
        [
            "--config",
            "/tmp/broker.yaml",
            "--query",
            "read",
            "--call-tool",
            "read-service.lookup",
            "--call-args",
            '{"limit": 1}',
        ]
    )

    assert args.config == "/tmp/broker.yaml"
    assert args.left_profile == "codex"
    assert args.right_profile == "claude"
    assert args.query == "read"
    assert args.call_tool == "read-service.lookup"
    assert args.call_args == '{"limit": 1}'

    overridden = _parse_args(
        [
            "--config",
            "/tmp/broker.yaml",
            "--left-profile",
            "left-client",
            "--right-profile",
            "right-client",
            "--query",
            "read",
            "--call-tool",
            "read-service.lookup",
            "--call-args",
            "{}",
        ]
    )
    assert overridden.left_profile == "left-client"
    assert overridden.right_profile == "right-client"

@pytest.mark.parametrize(
    "missing_flag,argv",
    [
        (
            "--config",
            [
                "--query",
                "read",
                "--call-tool",
                "read-service.lookup",
                "--call-args",
                "{}",
            ],
        ),
        (
            "--query",
            [
                "--config",
                "/tmp/broker.yaml",
                "--call-tool",
                "read-service.lookup",
                "--call-args",
                "{}",
            ],
        ),
        (
            "--call-tool",
            [
                "--config",
                "/tmp/broker.yaml",
                "--query",
                "read",
                "--call-args",
                "{}",
            ],
        ),
        (
            "--call-args",
            [
                "--config",
                "/tmp/broker.yaml",
                "--query",
                "read",
                "--call-tool",
                "read-service.lookup",
            ],
        ),
    ],
)
def test_parse_args_requires_probe_inputs(
    missing_flag: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(argv)

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert f"the following arguments are required: {missing_flag}" in captured.err
