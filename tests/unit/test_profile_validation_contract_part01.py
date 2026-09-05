from pathlib import Path
import pytest
import yaml
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

def test_build_profile_validation_plan_uses_enabled_yaml_upstreams_only(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(yaml.safe_dump(_plan_config(tmp_path), sort_keys=True), encoding="utf-8")

    plan = build_profile_validation_plan(BrokerConfig.from_file(config_path), "llm-profile")

    assert [probe.upstream_name for probe in plan.probes] == ["callable"]
    assert plan.missing_probes == ["missing", "missing-later"]

def test_search_probe_payload_uses_broad_limit_for_large_profiles() -> None:
    payload = _search_probe_payload("example.status")

    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "broker.search_tools"
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "broker.search_tools"
    assert payload["params"]["arguments"] == {
        "query": "example.status",
        "limit": 100,
    }

def test_parse_args_defaults_to_generic_codex_profile() -> None:
    args = _parse_args(["--config", "/tmp/example.yaml"])

    assert args.config == "/tmp/example.yaml"
    assert args.profile == "codex"

def test_parse_args_accepts_explicit_generic_profile() -> None:
    args = _parse_args(["--config", "/tmp/example.yaml", "--profile", "llm"])

    assert args.config == "/tmp/example.yaml"
    assert args.profile == "llm"

def test_parse_args_requires_config(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args([])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "the following arguments are required: --config" in captured.err

def test_parse_args_help_exposes_profile_validation_description(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "\nValidate all enabled profile upstreams with YAML smoke probes\n" in captured.out
    assert "XXValidate" not in captured.out

def test_normalize_probe_preserves_object_identity() -> None:
    probe = ProfileProbe(
        upstream_name="example",
        query="example status",
        tool="example.status",
        arguments={},
        call=False,
    )

    assert _normalize_probe(probe) is probe

def test_normalize_probe_defaults_optional_fields() -> None:
    probe = _normalize_probe(
        {
            "upstream_name": "example",
            "query": "example status",
            "tool": "example.status",
        }
    )

    assert probe == ProfileProbe(
        upstream_name="example",
        query="example status",
        tool="example.status",
        arguments={},
        call=True,
    )

def test_run_profile_validation_exercises_each_configured_probe() -> None:
    report = run_profile_validation(
        socket_path=Path("/tmp/unused.sock"),
        profile="llm-profile",
        probes=[
            {
                "upstream_name": "callable",
                "query": "callable status",
                "tool": "callable.status",
                "arguments": {},
                "call": True,
            },
            {
                "upstream_name": "search-only",
                "query": "search only",
                "tool": "search-only.lookup",
                "arguments": {},
                "call": False,
            },
        ],
        missing_probes=[],
        session_id="session",
        request_fn=SequenceRequester(_success_responses()),
    )

    assert report["matches"] is True
    assert set(report) == {
        "matches",
        "profile",
        "advertised_tools",
        "visible_upstreams",
        "validated_upstreams",
        "missing_probes",
        "probe_results",
    }
    assert report["profile"] == "llm-profile"
    assert report["advertised_tools"] == ["broker.search_tools", "broker.status"]
    assert report["visible_upstreams"] == ["callable", "search-only"]
    assert report["missing_probes"] == []
    assert report["validated_upstreams"] == ["callable", "search-only"]
    assert report["probe_results"]["callable"]["called"] is True
    assert report["probe_results"]["callable"]["call_output_bytes"] == len('{"ok": true}')
    assert "call_text" not in report["probe_results"]["callable"]
    assert report["probe_results"]["search-only"]["called"] is False
    assert report["probe_results"]["search-only"]["call_output_bytes"] == 0

def test_run_single_probe_forwards_context_and_returns_probe_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.profile_validation as profile_validation

    socket_path = Path("/tmp/profile-validation.sock")
    upstreams = {"callable": {"exposed": True, "state": "reachable"}}
    final_upstreams = {"callable": {"exposed": True, "state": "running"}}
    probe = ProfileProbe(
        upstream_name="callable",
        query="callable status",
        tool="callable.status",
        arguments={"check": True},
    )
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def require(
        profile: str,
        snapshot: dict[str, object],
        upstream_name: str,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(("require", (profile, snapshot, upstream_name), kwargs))
        return {"state": "reachable"}

    def search(**kwargs: object) -> tuple[list[str], str, dict[str, object]]:
        calls.append(("search", (), kwargs))
        return ["callable.status"], "callable.status", {"type": "object"}

    def call(**kwargs: object) -> dict[str, object]:
        calls.append(("call", (), kwargs))
        return {"called": True, "call_content_items": 1, "call_output_bytes": 2}

    def load_status(*args: object) -> dict[str, object]:
        calls.append(("load_status", args, {}))
        return final_upstreams

    monkeypatch.setattr(profile_validation, "_require_exposed_upstream", require)
    monkeypatch.setattr(profile_validation, "_search_and_describe_probe", search)
    monkeypatch.setattr(profile_validation, "_call_probe_if_enabled", call)
    monkeypatch.setattr(profile_validation, "_load_upstream_status", load_status)

    result = _run_single_probe(
        socket_path=socket_path,
        profile="llm-profile",
        session_id="session",
        upstreams=upstreams,
        probe=probe,
        request_fn=lambda *_args, **_kwargs: {},
    )

    assert result == {
        "state": "reachable",
        "search_matches": ["callable.status"],
        "described_tool": "callable.status",
        "called": True,
        "call_content_items": 1,
        "call_output_bytes": 2,
    }
    assert calls[0] == (
        "require",
        ("llm-profile", upstreams, "callable"),
        {"context": "", "require_active": False},
    )
    assert calls[1] == (
        "search",
        (),
        {
            "socket_path": socket_path,
            "profile": "llm-profile",
            "session_id": "session",
            "probe": probe,
            "request_fn": calls[1][2]["request_fn"],
        },
    )
    assert calls[2] == (
        "call",
        (),
        {
            "socket_path": socket_path,
            "profile": "llm-profile",
            "session_id": "session",
            "probe": probe,
            "input_schema": {"type": "object"},
            "request_fn": calls[2][2]["request_fn"],
        },
    )
    assert calls[3] == ("load_status", (socket_path, "llm-profile", "session", calls[3][1][3]), {})
    assert calls[4] == (
        "require",
        ("llm-profile", final_upstreams, "callable"),
        {"context": "after probe", "require_active": True},
    )

def test_run_profile_validation_sends_expected_client_requests() -> None:
    socket_path = Path("/tmp/profile-validation.sock")
    requester = RecordingSequenceRequester(_success_responses())

    report = run_profile_validation(
        socket_path=socket_path,
        profile="llm-profile",
        probes=[
            {
                "upstream_name": "callable",
                "query": "callable status",
                "tool": "callable.status",
                "arguments": {},
                "call": True,
            },
            {
                "upstream_name": "search-only",
                "query": "search only",
                "tool": "search-only.lookup",
                "arguments": {},
                "call": False,
            },
        ],
        missing_probes=[],
        session_id="session",
        request_fn=requester,
    )

    assert report["validated_upstreams"] == ["callable", "search-only"]
    assert [call_args[:3] for call_args, _kwargs in requester.calls] == [
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
        (socket_path, "llm-profile", "session"),
    ]
    payloads = [call_args[3] for call_args, _kwargs in requester.calls]
    assert [payload["method"] for payload in payloads] == [
        "initialize",
        "tools/call",
        "tools/list",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
        "tools/call",
    ]
    assert payloads[1]["params"]["name"] == "broker.status"
    assert payloads[4]["params"]["arguments"] == {"query": "callable status", "limit": 100}
    assert payloads[5]["params"]["arguments"] == {"name": "callable.status"}
    assert payloads[6]["params"]["arguments"] == {
        "name": "callable.status",
        "arguments": {},
    }
    assert payloads[8]["params"]["arguments"] == {"query": "search only", "limit": 100}
    assert payloads[9]["params"]["arguments"] == {"name": "search-only.lookup"}

def test_run_profile_validation_reports_missing_probes_in_sorted_order() -> None:
    with pytest.raises(DiscoveryParityError, match="llm-profile missing smoke probes: alpha, zebra"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[],
            missing_probes=["zebra", "alpha"],
            session_id="session",
            request_fn=SequenceRequester([]),
        )

def test_run_profile_validation_ignores_hidden_and_malformed_upstreams_in_visibility() -> None:
    responses = [
        {"result": {}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "hidden": {"exposed": False, "state": "running"},
                        "malformed": "not a snapshot",
                    }
                }
            }
        },
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {
            "result": {
                "structuredContent": {
                    "upstreams": {
                        "callable": {"exposed": True, "state": "running"},
                        "hidden": {"exposed": False, "state": "running"},
                        "malformed": "not a snapshot",
                    }
                }
            }
        },
    ]

    report = run_profile_validation(
        socket_path=Path("/tmp/unused.sock"),
        profile="llm-profile",
        probes=[],
        missing_probes=[],
        session_id="session",
        request_fn=SequenceRequester(responses),
    )

    assert report["visible_upstreams"] == ["callable"]

def test_run_profile_validation_accepts_profile_probe_objects() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        {"result": {"structuredContent": {"tool": {"name": "callable.status"}}}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
    ]

    report = run_profile_validation(
        socket_path=Path("/tmp/unused.sock"),
        profile="llm-profile",
        probes=[
            ProfileProbe(
                upstream_name="callable",
                query="callable status",
                tool="callable.status",
                arguments={},
                call=False,
            )
        ],
        missing_probes=[],
        session_id="session",
        request_fn=SequenceRequester(responses),
    )

    assert report["validated_upstreams"] == ["callable"]
    assert report["probe_results"]["callable"]["called"] is False

def test_call_probe_if_enabled_counts_multiple_content_items() -> None:
    requester = RecordingSequenceRequester(
        [
            {
                "result": {
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ]
                }
            }
        ]
    )

    result = _call_probe_if_enabled(
        socket_path=Path("/tmp/profile-validation.sock"),
        profile="llm-profile",
        session_id="session",
        probe=ProfileProbe(
            upstream_name="callable",
            query="callable status",
            tool="callable.status",
            arguments={"enabled": True},
        ),
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
        request_fn=requester,
    )

    assert result == {"called": True, "call_content_items": 2, "call_output_bytes": 11}
    payload = requester.calls[0][0][3]
    assert payload["params"]["arguments"] == {
        "name": "callable.status",
        "arguments": {"enabled": True},
    }

def test_call_probe_if_enabled_passes_profile_to_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.profile_validation as profile_validation

    seen: list[tuple[str, ProfileProbe, dict[str, object] | None, dict[str, object]]] = []
    probe = ProfileProbe(
        upstream_name="callable",
        query="callable status",
        tool="callable.status",
        arguments={},
    )

    def validate(
        profile: str,
        validated_probe: ProfileProbe,
        input_schema: dict[str, object] | None,
        arguments: dict[str, object],
    ) -> None:
        seen.append((profile, validated_probe, input_schema, arguments))

    monkeypatch.setattr(profile_validation, "_validate_probe_arguments", validate)

    result = _call_probe_if_enabled(
        socket_path=Path("/tmp/profile-validation.sock"),
        profile="llm-profile",
        session_id="session",
        probe=probe,
        input_schema={"type": "object"},
        request_fn=SequenceRequester([{"result": {}}]),
    )

    assert result == {"called": True, "call_content_items": 0, "call_output_bytes": 0}
    assert seen == [("llm-profile", probe, {"type": "object"}, {})]

def test_call_probe_if_enabled_handles_missing_content_as_empty_result() -> None:
    result = _call_probe_if_enabled(
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
        request_fn=SequenceRequester([{"result": {}}]),
    )

    assert result == {"called": True, "call_content_items": 0, "call_output_bytes": 0}

def test_call_probe_if_enabled_reports_empty_error_text_when_error_has_no_content() -> None:
    with pytest.raises(DiscoveryParityError, match=r"probe returned upstream error: $"):
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
            request_fn=SequenceRequester([{"result": {"isError": True}}]),
        )

def test_call_probe_if_enabled_skips_search_only_probe_without_schema() -> None:
    requester = RecordingSequenceRequester([])

    result = _call_probe_if_enabled(
        socket_path=Path("/tmp/profile-validation.sock"),
        profile="llm-profile",
        session_id="session",
        probe=ProfileProbe(
            upstream_name="search-only",
            query="search only",
            tool="search-only.lookup",
            arguments={},
            call=False,
        ),
        input_schema=None,
        request_fn=requester,
    )

    assert result == {"called": False, "call_content_items": 0, "call_output_bytes": 0}
    assert requester.calls == []
