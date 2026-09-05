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

def test_search_and_describe_probe_rejects_missing_matches() -> None:
    with pytest.raises(DiscoveryParityError, match="search did not return callable.status"):
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
            request_fn=SequenceRequester([{"result": {"structuredContent": {}}}]),
        )

def test_run_profile_validation_fails_when_describe_returns_wrong_tool() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        {"result": {"structuredContent": {"tool": {"name": "callable.other"}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="describe returned callable.other"):
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

def test_run_profile_validation_fails_when_called_probe_has_no_described_input_schema() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        {"result": {"structuredContent": {"tool": {"name": "callable.status"}}}},
    ]
    with pytest.raises(DiscoveryParityError, match="describe returned invalid inputSchema"):
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

def test_run_profile_validation_validates_called_probe_arguments_against_described_schema() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        {
            "result": {
                "structuredContent": {
                    "tool": {
                        "name": "callable.status",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"code": {"type": "string"}},
                            "required": ["code"],
                            "additionalProperties": False,
                        },
                    }
                }
            }
        },
    ]
    with pytest.raises(DiscoveryParityError, match="'code' is a required property"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": {"wrong_name": "value"},
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_on_bad_probe_arguments() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        _describe_response("callable.status"),
    ]
    with pytest.raises(DiscoveryParityError, match="probe arguments must be a mapping"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[
                {
                    "upstream_name": "callable",
                    "query": "callable status",
                    "tool": "callable.status",
                    "arguments": [],
                    "call": True,
                }
            ],
            missing_probes=[],
            session_id="session",
            request_fn=SequenceRequester(responses),
        )

def test_run_profile_validation_fails_on_tool_level_error() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.status"}]}}},
        _describe_response("callable.status"),
        {"result": {"isError": True, "content": [{"type": "text", "text": "Error: denied"}]}},
    ]
    with pytest.raises(DiscoveryParityError, match="probe returned upstream error"):
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

def test_run_profile_validation_fails_when_yaml_upstream_lacks_probe() -> None:
    with pytest.raises(DiscoveryParityError, match="missing smoke probes: missing"):
        run_profile_validation(
            socket_path=Path("/tmp/unused.sock"),
            profile="llm-profile",
            probes=[],
            missing_probes=["missing"],
            session_id="session",
            request_fn=SequenceRequester([]),
        )

def test_run_profile_validation_fails_when_search_does_not_find_probe_tool() -> None:
    responses = [
        {"result": {}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"tools": [{"name": "broker.search_tools"}]}},
        {"result": {"structuredContent": {"upstreams": {"callable": {"exposed": True, "state": "running"}}}}},
        {"result": {"structuredContent": {"matches": [{"name": "callable.other"}]}}},
    ]
    with pytest.raises(DiscoveryParityError, match="search did not return callable.status"):
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

def test_profile_validation_main_reports_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.profile_validation as profile_validation

    def raise_error(_args: object) -> dict:
        raise profile_validation.DiscoveryParityError("profile failed")

    monkeypatch.setattr(profile_validation, "_run_validation", raise_error)

    result = profile_validation.main(["--config", "/tmp/broker.yaml", "--profile", "llm"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "profile failed\n"

def test_profile_validation_main_parses_args_and_writes_sorted_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.profile_validation as profile_validation

    seen_args: list[object] = []

    def run_validation(args: object) -> dict[str, object]:
        seen_args.append(args)
        return {"profile": args.profile, "matches": True}

    monkeypatch.setattr(profile_validation, "_run_validation", run_validation)

    result = profile_validation.main(["--config", "/tmp/broker.yaml", "--profile", "llm"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == '{"matches": true, "profile": "llm"}\n'
    assert captured.err == ""
    assert seen_args[0].config == "/tmp/broker.yaml"
    assert seen_args[0].profile == "llm"

def test_profile_validation_stops_session_when_existing_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.profile_validation as profile_validation
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(yaml.safe_dump(_plan_config(tmp_path), sort_keys=True), encoding="utf-8")
    stopped: list[tuple[Path, str, str]] = []
    broker_daemon_error = profile_validation._start_daemon_if_needed.__globals__["BrokerDaemonError"]

    class AlreadyRunningDaemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise broker_daemon_error("broker daemon already running")

    monkeypatch.setattr(profile_validation, "BrokerDaemon", AlreadyRunningDaemon)
    monkeypatch.setattr(profile_validation, "build_profile_validation_plan", lambda _config, _profile: type("Plan", (), {"probes": [], "missing_probes": []})())
    monkeypatch.setattr(
        profile_validation,
        "run_profile_validation",
        lambda **_kwargs: {"matches": True, "validated_upstreams": []},
    )
    monkeypatch.setattr(
        profile_validation,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: stopped.append((socket_path, profile, session_id)),
    )

    report = profile_validation._run_validation(
        type("Args", (), {"config": str(config_path), "profile": "llm"})()
    )

    config = BrokerConfig.from_file(config_path)
    assert report == {"matches": True, "validated_upstreams": [], "started_daemon": False}
    assert stopped[0][0] == config.runtime.socket_path
    assert stopped[0][1] == "llm"

def test_run_validation_wires_config_plan_session_and_existing_daemon_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.profile_validation as profile_validation
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(yaml.safe_dump(_plan_config(tmp_path), sort_keys=True), encoding="utf-8")
    config = BrokerConfig.from_file(config_path)
    probe = ProfileProbe(
        upstream_name="callable",
        query="callable status",
        tool="callable.status",
        arguments={},
    )
    missing_probes = ["missing"]
    daemon_kwargs: list[dict[str, object]] = []
    validation_kwargs: list[dict[str, object]] = []
    stopped: list[tuple[Path, str, str]] = []

    class RecordingDaemon:
        def __init__(self, **kwargs: object) -> None:
            daemon_kwargs.append(kwargs)

    def build_plan(plan_config: BrokerConfig, profile: str) -> object:
        assert plan_config.runtime.root == config.runtime.root
        assert profile == "llm"
        return type("Plan", (), {"probes": (probe,), "missing_probes": missing_probes})()

    def validate(**kwargs: object) -> dict[str, object]:
        validation_kwargs.append(kwargs)
        assert kwargs["socket_path"] == config.runtime.socket_path
        assert kwargs["profile"] == "llm"
        assert kwargs["probes"] == (probe,)
        assert kwargs["missing_probes"] is missing_probes
        assert isinstance(kwargs["session_id"], str)
        assert str(kwargs["session_id"]).startswith("profile-validation-llm-")
        return {"matches": True, "validated_upstreams": ["callable"]}

    monkeypatch.setattr(profile_validation, "BrokerDaemon", RecordingDaemon)
    monkeypatch.setattr(profile_validation, "build_profile_validation_plan", build_plan)
    monkeypatch.setattr(profile_validation, "_start_daemon_if_needed", lambda daemon: False)
    monkeypatch.setattr(profile_validation, "run_profile_validation", validate)
    monkeypatch.setattr(
        profile_validation,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: stopped.append((socket_path, profile, session_id)),
    )

    report = profile_validation._run_validation(
        type("Args", (), {"config": str(config_path), "profile": "llm"})()
    )

    assert report == {
        "matches": True,
        "validated_upstreams": ["callable"],
        "started_daemon": False,
    }
    assert len(daemon_kwargs) == 1
    assert daemon_kwargs[0]["runtime_root"] == config.runtime.root
    assert daemon_kwargs[0]["socket_path"] == config.runtime.socket_path
    assert isinstance(daemon_kwargs[0]["broker_config"], BrokerConfig)
    assert daemon_kwargs[0]["broker_config"].runtime.root == config.runtime.root
    assert stopped == [
        (config.runtime.socket_path, "llm", validation_kwargs[0]["session_id"]),
    ]

def test_run_validation_stops_started_daemon_with_broker_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.profile_validation as profile_validation
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(yaml.safe_dump(_plan_config(tmp_path), sort_keys=True), encoding="utf-8")
    config = BrokerConfig.from_file(config_path)
    events: list[tuple[str, object]] = []

    class RecordingDaemon:
        def __init__(self, **kwargs: object) -> None:
            events.append(("init", kwargs))

        def join(self, timeout: int) -> None:
            events.append(("join", timeout))

        def stop(self) -> None:
            events.append(("stop", None))

    monkeypatch.setattr(profile_validation, "BrokerDaemon", RecordingDaemon)
    monkeypatch.setattr(
        profile_validation,
        "build_profile_validation_plan",
        lambda _config, _profile: type("Plan", (), {"probes": (), "missing_probes": []})(),
    )
    monkeypatch.setattr(profile_validation, "_start_daemon_if_needed", lambda daemon: True)
    monkeypatch.setattr(
        profile_validation,
        "run_profile_validation",
        lambda **_kwargs: {"matches": True, "validated_upstreams": []},
    )
    monkeypatch.setattr(
        profile_validation,
        "_request_through_client",
        lambda **kwargs: events.append(("request", kwargs)),
    )

    report = profile_validation._run_validation(
        type("Args", (), {"config": str(config_path), "profile": "llm"})()
    )

    assert report == {"matches": True, "validated_upstreams": [], "started_daemon": True}
    assert events[0] == (
        "init",
        {
            "runtime_root": config.runtime.root,
            "socket_path": config.runtime.socket_path,
            "broker_config": BrokerConfig.from_file(config_path),
        },
    )
    assert events[1] == (
        "request",
        {
            "socket_path": config.runtime.socket_path,
            "profile": "llm",
            "session_id": "profile-validation-stop",
            "payload": {"jsonrpc": "2.0", "id": "stop", "method": "broker/stop"},
        },
    )
    assert events[2:] == [("join", 5), ("stop", None)]
