import json
from argparse import Namespace
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

def test_parse_args_help_describes_discovery_parity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "\nCompare broker discovery across profiles\n\noptions:" in captured.out

def test_discovery_parity_main_returns_zero_and_json_report_for_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    monkeypatch.setattr(
        discovery_parity,
        "_run_parity",
        lambda _args: {
            "matches": True,
            "mismatches": [],
            "profiles": {"left-client": {"profile": "left-client"}},
            "started_daemon": True,
        },
    )

    result = discovery_parity.main(
        [
            "--config",
            "/tmp/broker.yaml",
            "--left-profile",
            "left-client",
            "--right-profile",
            "right-client",
            "--query",
            "echo",
            "--call-tool",
            "echo.echo",
            "--call-args",
            "{}",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out) == {
        "matches": True,
        "mismatches": [],
        "profiles": {"left-client": {"profile": "left-client"}},
        "started_daemon": True,
    }
    assert captured.err == ""

def test_discovery_parity_main_passes_parsed_args_to_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    parsed_args = Namespace(config="/tmp/broker.yaml")
    seen: dict[str, object] = {}

    def fake_run(args: Namespace) -> dict[str, object]:
        seen["args"] = args
        return {"matches": True, "mismatches": [], "profiles": {}, "started_daemon": False}

    monkeypatch.setattr(discovery_parity, "_parse_args", lambda argv: parsed_args)
    monkeypatch.setattr(discovery_parity, "_run_parity", fake_run)

    assert discovery_parity.main(["--config", "/tmp/broker.yaml"]) == 0
    assert seen["args"] is parsed_args
    assert json.loads(capsys.readouterr().out)["matches"] is True

def test_discovery_parity_main_writes_stable_sorted_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    monkeypatch.setattr(discovery_parity, "_run_parity", lambda _args: {"z": 1, "matches": True, "a": 2})

    result = discovery_parity.main(
        [
            "--config",
            "/tmp/broker.yaml",
            "--query",
            "echo",
            "--call-tool",
            "echo.echo",
            "--call-args",
            "{}",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == '{"a": 2, "matches": true, "z": 1}\n'

def test_discovery_parity_main_returns_one_for_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    monkeypatch.setattr(
        discovery_parity,
        "_run_parity",
        lambda _args: {
            "matches": False,
            "mismatches": ["advertised_tools mismatch"],
            "profiles": {},
            "started_daemon": False,
        },
    )

    result = discovery_parity.main(
        [
            "--config",
            "/tmp/broker.yaml",
            "--query",
            "echo",
            "--call-tool",
            "echo.echo",
            "--call-args",
            "{}",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert json.loads(captured.out)["mismatches"] == ["advertised_tools mismatch"]

def test_discovery_parity_main_reports_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.discovery_parity as discovery_parity

    def raise_error(_args: object) -> dict:
        raise discovery_parity.DiscoveryParityError("bad parity")

    monkeypatch.setattr(discovery_parity, "_run_parity", raise_error)

    result = discovery_parity.main(
        [
            "--config",
            "/tmp/broker.yaml",
            "--query",
            "echo",
            "--call-tool",
            "echo.echo",
            "--call-args",
            "{}",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "bad parity\n"

def test_run_parity_builds_profile_reports_and_cleans_existing_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    seen: dict[str, object] = {"discoveries": [], "stopped": []}

    class FakeDaemon:
        def __init__(self, *, runtime_root: Path, socket_path: Path, broker_config: BrokerConfig):
            seen["daemon_init"] = (runtime_root, socket_path, broker_config)

    def fake_discovery(**kwargs: object) -> dict[str, object]:
        cast_calls = seen["discoveries"]
        assert isinstance(cast_calls, list)
        cast_calls.append(kwargs)
        return {
            "profile": kwargs["profile"],
            "advertised_tools": ["broker.search_tools"],
            "visible_upstreams": ["read-service"],
            "search_matches": ["read-service.lookup"],
            "described_tool": "read-service.lookup",
            "call_text": '{"ok": true}',
        }

    monkeypatch.setattr(discovery_parity.BrokerConfig, "from_file", lambda _path: config)
    monkeypatch.setattr(discovery_parity, "BrokerDaemon", FakeDaemon)
    monkeypatch.setattr(discovery_parity, "_start_daemon_if_needed", lambda _daemon: False)
    monkeypatch.setattr(discovery_parity, "run_profile_discovery", fake_discovery)
    monkeypatch.setattr(
        discovery_parity,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: seen["stopped"].append(
            (socket_path, profile, session_id)
        ),
    )

    report = discovery_parity._run_parity(
        Namespace(
            config=str(tmp_path / "broker.yaml"),
            left_profile="left-client",
            right_profile="right-client",
            query="read",
            call_tool="read-service.lookup",
            call_args='{"limit": 1}',
        )
    )

    assert seen["daemon_init"] == (config.runtime.root, config.runtime.socket_path, config)
    assert report["matches"] is True
    assert report["started_daemon"] is False
    discoveries = seen["discoveries"]
    assert isinstance(discoveries, list)
    assert [call["profile"] for call in discoveries] == ["left-client", "right-client"]
    assert [call["call_args"] for call in discoveries] == [{"limit": 1}, {"limit": 1}]
    assert all(call["socket_path"] == config.runtime.socket_path for call in discoveries)
    assert all(str(call["session_id"]).startswith("discovery-parity-") for call in discoveries)
    assert len({call["session_id"] for call in discoveries}) == 2
    assert seen["stopped"] == [
        (config.runtime.socket_path, "left-client", discoveries[0]["session_id"]),
        (config.runtime.socket_path, "right-client", discoveries[1]["session_id"]),
    ]

def test_run_parity_passes_config_path_daemon_and_probe_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    seen: dict[str, object] = {"discoveries": []}

    class FakeDaemon:
        pass

    def fake_from_file(path: Path) -> BrokerConfig:
        seen["config_path"] = path
        return config

    def fake_daemon(*, runtime_root: Path, socket_path: Path, broker_config: BrokerConfig) -> FakeDaemon:
        daemon = FakeDaemon()
        seen["daemon"] = daemon
        seen["daemon_init"] = (runtime_root, socket_path, broker_config)
        return daemon

    def fake_start(daemon: FakeDaemon) -> bool:
        seen["started_daemon_arg"] = daemon
        return True

    def fake_discovery(**kwargs: object) -> dict[str, object]:
        calls = seen["discoveries"]
        assert isinstance(calls, list)
        calls.append(kwargs)
        return {
            "profile": kwargs["profile"],
            "advertised_tools": ["broker.search_tools"],
            "visible_upstreams": ["read-service"],
            "search_matches": ["read-service.lookup"],
            "described_tool": "read-service.lookup",
            "call_text": '{"ok": true}',
        }

    monkeypatch.setattr(discovery_parity.BrokerConfig, "from_file", fake_from_file)
    monkeypatch.setattr(discovery_parity, "BrokerDaemon", fake_daemon)
    monkeypatch.setattr(discovery_parity, "_start_daemon_if_needed", fake_start)
    monkeypatch.setattr(discovery_parity, "parse_call_args", lambda text: {"parsed": text})
    monkeypatch.setattr(discovery_parity, "run_profile_discovery", fake_discovery)
    monkeypatch.setattr(
        discovery_parity,
        "_cleanup_parity_daemon",
        lambda config_arg, args_arg, daemon_arg, started_arg, left_id, right_id: seen.update(
            {
                "cleanup": (
                    config_arg,
                    args_arg,
                    daemon_arg,
                    started_arg,
                    left_id,
                    right_id,
                )
            }
        ),
    )

    args = Namespace(
        config=str(tmp_path / "broker.yaml"),
        left_profile="left-client",
        right_profile="right-client",
        query="read-service",
        call_tool="read-service.lookup",
        call_args='{"limit": 1}',
    )
    report = _run_parity(args)

    assert seen["config_path"] == tmp_path / "broker.yaml"
    assert seen["daemon_init"] == (config.runtime.root, config.runtime.socket_path, config)
    assert seen["started_daemon_arg"] is seen["daemon"]
    discoveries = seen["discoveries"]
    assert isinstance(discoveries, list)
    assert [
        (
            call["profile"],
            call["query"],
            call["call_tool"],
            call["call_args"],
        )
        for call in discoveries
    ] == [
        ("left-client", "read-service", "read-service.lookup", {"parsed": '{"limit": 1}'}),
        ("right-client", "read-service", "read-service.lookup", {"parsed": '{"limit": 1}'}),
    ]
    cleanup = seen["cleanup"]
    assert isinstance(cleanup, tuple)
    assert cleanup[:4] == (config, args, seen["daemon"], True)
    assert report["matches"] is True
    assert report["started_daemon"] is True

def test_run_parity_cleans_with_not_started_state_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    seen: dict[str, object] = {}

    class FakeDaemon:
        pass

    monkeypatch.setattr(discovery_parity.BrokerConfig, "from_file", lambda _path: config)
    monkeypatch.setattr(discovery_parity, "BrokerDaemon", lambda **_kwargs: FakeDaemon())
    monkeypatch.setattr(
        discovery_parity,
        "_start_daemon_if_needed",
        lambda _daemon: (_ for _ in ()).throw(DiscoveryParityError("cannot start")),
    )
    monkeypatch.setattr(
        discovery_parity,
        "_cleanup_parity_daemon",
        lambda _config, _args, _daemon, started, _left_id, _right_id: seen.update(
            {"started": started}
        ),
    )

    with pytest.raises(DiscoveryParityError, match="cannot start"):
        _run_parity(
            Namespace(
                config=str(tmp_path / "broker.yaml"),
                left_profile="left-client",
                right_profile="right-client",
                query="read",
                call_tool="read-service.lookup",
                call_args="{}",
            )
        )

    assert seen["started"] is False

def test_discovery_parity_cleanup_stops_sessions_for_existing_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    stopped: list[tuple[Path, str, str]] = []
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    args = Namespace(left_profile="left", right_profile="right")
    monkeypatch.setattr(
        discovery_parity,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: stopped.append((socket_path, profile, session_id)),
    )

    discovery_parity._cleanup_parity_daemon(
        config,
        args,
        daemon=object(),
        started_daemon=False,
        left_session_id="left-session",
        right_session_id="right-session",
    )

    assert stopped == [
        (tmp_path / "broker.sock", "left", "left-session"),
        (tmp_path / "broker.sock", "right", "right-session"),
    ]

def test_discovery_parity_cleanup_stops_started_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.discovery_parity as discovery_parity
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    calls: list[tuple[str, object]] = []

    class FakeDaemon:
        def join(self, timeout: int) -> None:
            calls.append(("join", timeout))

        def stop(self) -> None:
            calls.append(("stop", None))

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    args = Namespace(left_profile="left-client", right_profile="right-client")
    monkeypatch.setattr(
        discovery_parity,
        "_request_through_client",
        lambda **kwargs: calls.append(("request", kwargs)) or {"result": {}},
    )

    discovery_parity._cleanup_parity_daemon(
        config,
        args,
        daemon=FakeDaemon(),
        started_daemon=True,
        left_session_id="left-session",
        right_session_id="right-session",
    )

    assert calls == [
        (
            "request",
            {
                "socket_path": config.runtime.socket_path,
                "profile": "left-client",
                "session_id": "discovery-parity-stop",
                "payload": {"jsonrpc": "2.0", "id": "stop", "method": "broker/stop"},
            },
        ),
        ("join", 5),
        ("stop", None),
    ]
