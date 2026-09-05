from pathlib import Path
import json
import pytest
from mcp_broker.config import BrokerConfig
from mcp_broker.schema import DEFAULT_CALL_TIMEOUT_SECONDS
pytestmark = pytest.mark.unit
class RaisingClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        raise self.exception
class CreatedClient:
    def __init__(self, client_id: int) -> None:
        self.client_id = client_id
        self.call_calls: list[tuple[str, dict[str, object], int]] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        return {"content": [{"type": "text", "text": f"client-{self.client_id}"}]}

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "running",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": None,
        }

    def stop(self) -> list[int]:
        return []
class RecoverableExitedClient:
    def __init__(self, restart_error: Exception | None = None) -> None:
        self.restart_error = restart_error
        self.ensure_running_calls = 0
        self.running = False
        self.last_error: str | None = None

    def ensure_running(self) -> None:
        self.ensure_running_calls += 1
        if self.restart_error is not None:
            self.last_error = str(self.restart_error)
            raise self.restart_error
        self.running = True
        self.last_error = None

    def health_snapshot(self) -> dict[str, object]:
        if not self.running:
            return {
                "state": "exited",
                "pid": None,
                "cpu_percent": None,
                "memory_mb": None,
                "restarts": 0,
                "last_error": self.last_error,
            }
        return {
            "state": "running",
            "pid": 12345,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 1,
            "last_error": None,
        }
class RaisingHttpClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        raise self.exception
class HealthHttpClient:
    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "reachable",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": None,
        }
class ListingClient:
    def __init__(self, tools: list[dict[str, object]]) -> None:
        self.tools = tools

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        assert timeout_seconds == DEFAULT_CALL_TIMEOUT_SECONDS
        return self.tools
class ListErrorClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        assert timeout_seconds == DEFAULT_CALL_TIMEOUT_SECONDS
        raise self.exception
class CatalogClient:
    def __init__(self, tools: list[dict[str, object]], response: dict[str, object]) -> None:
        self.tools = tools
        self.response = response
        self.list_calls: list[int] = []
        self.call_calls: list[tuple[str, dict[str, object], int]] = []

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.list_calls.append(timeout_seconds)
        return self.tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        return self.response
class SequenceClient:
    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = responses
        self.call_calls: list[tuple[str, dict[str, object], int]] = []
        self.last_error: str | None = None

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            self.last_error = str(response)
            raise response
        self.last_error = None
        return response

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "running",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": self.last_error,
        }
class BufferConnection:
    def __init__(self, received: bytes) -> None:
        self.received = received
        self.sent = b""

    def recv(self, _size: int) -> bytes:
        return self.received

    def sendall(self, data: bytes) -> None:
        self.sent += data
class _ContextConnection:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "_ContextConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None
def _empty_config(tmp_path: Path) -> BrokerConfig:
    from mcp_broker.config import BrokerSettings, RuntimeConfig

    return BrokerConfig(
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

@pytest.mark.error_simulation
def test_daemon_cleanup_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    shutdown_calls: list[str] = []
    snapshots: list[str] = []

    def shutdown_upstreams() -> dict[str, object]:
        shutdown_calls.append("called")
        return {"stopped_upstreams": [], "remaining_broker_processes": []}

    monkeypatch.setattr(daemon, "_shutdown_upstreams", shutdown_upstreams)
    monkeypatch.setattr(daemon, "_write_status_snapshot", snapshots.append)

    daemon._cleanup()
    daemon._cleanup()

    assert shutdown_calls == ["called"]
    assert snapshots == ["stopped"]

def test_daemon_cli_status_uses_health_method() -> None:
    from mcp_broker.daemon import _broker_method_for_command
    from mcp_broker.daemon_cli import _broker_method_for_command as cli_method_for_command

    assert _broker_method_for_command("status") == "broker/health"
    assert _broker_method_for_command("stop") == "broker/stop"
    assert cli_method_for_command("status") == "broker/health"
    assert cli_method_for_command("stop") == "broker/stop"

def test_daemon_cli_loads_config_for_serve(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.daemon import _broker_config_for_serve
    from mcp_broker.daemon_cli import _broker_config_for_serve as cli_broker_config_for_serve

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"root": str(tmp_path / "runtime")},
                "upstreams": {"read-store": {"command": "read-store", "tool_prefix": "read-store"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    config = _broker_config_for_serve(config_path)
    cli_config = cli_broker_config_for_serve(config_path)

    assert config is not None
    assert cli_config is not None
    assert config.runtime.root == tmp_path / "runtime"
    assert cli_config.runtime.root == tmp_path / "runtime"
    assert sorted(config.upstreams) == ["read-store"]
    assert sorted(cli_config.upstreams) == ["read-store"]

def test_daemon_cli_keeps_legacy_serve_without_config() -> None:
    from mcp_broker.daemon import _broker_config_for_serve
    from mcp_broker.daemon_cli import _broker_config_for_serve as cli_broker_config_for_serve

    assert _broker_config_for_serve(None) is None
    assert cli_broker_config_for_serve(None) is None

def test_daemon_cli_rejects_unknown_command_and_missing_required_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    with pytest.raises(SystemExit) as unknown:
        main(["restart", "--runtime-root", "/tmp/runtime", "--socket-path", "/tmp/broker.sock"])
    with pytest.raises(SystemExit) as missing_runtime:
        main(["status", "--socket-path", "/tmp/broker.sock"])
    with pytest.raises(SystemExit) as missing_socket:
        main(["status", "--runtime-root", "/tmp/runtime"])

    assert unknown.value.code == 2
    assert missing_runtime.value.code == 2
    assert missing_socket.value.code == 2
    error_text = capsys.readouterr().err
    assert "invalid choice: 'restart'" in error_text
    assert "--runtime-root" in error_text
    assert "--socket-path" in error_text

def test_daemon_cli_help_includes_description(capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.daemon_cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "\nRun and inspect mcp-broker daemon\n" in output
    assert "XXRun" not in output
    assert "serve" in output
    assert "status" in output
    assert "stop" in output

def test_daemon_cli_status_and_stop_do_not_enter_serve_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status and stop must not construct daemon")

    requests: list[tuple[Path, str]] = []

    def request_fn(socket_path: Path, method: str) -> dict[str, object]:
        requests.append((socket_path, method))
        return {"id": method, "result": {"ok": True}}

    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / "broker.sock"

    assert (
        main(
            ["status", "--runtime-root", str(runtime_root), "--socket-path", str(socket_path)],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )
    assert (
        main(
            ["stop", "--runtime-root", str(runtime_root), "--socket-path", str(socket_path)],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )

    assert requests == [(socket_path, "broker/health"), (socket_path, "broker/stop")]
    assert [json.loads(line)["result"] for line in capsys.readouterr().out.splitlines()] == [
        {"ok": True},
        {"ok": True},
    ]

def test_daemon_cli_writes_sorted_json_response(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status must not construct daemon")

    def request_fn(_socket_path: Path, method: str) -> dict[str, object]:
        return {"z": 1, "id": method, "a": 2}

    assert (
        main(
            [
                "status",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )

    assert capsys.readouterr().out == '{"a": 2, "id": "broker/health", "z": 1}\n'

@pytest.mark.error_simulation
def test_daemon_cli_client_request_uses_unix_stream_socket_and_exact_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon_cli as daemon_cli

    events: list[tuple[str, object]] = []

    class FakeSocket:
        def __init__(self, family: object, kind: object) -> None:
            events.append(("init", (family, kind)))
            self._chunks = [b'{"result": ', b'{"ok": true}}', b""]

        def __enter__(self) -> "FakeSocket":
            events.append(("enter", None))
            return self

        def __exit__(self, *_args: object) -> None:
            events.append(("exit", None))

        def connect(self, address: str) -> None:
            events.append(("connect", address))

        def sendall(self, payload: bytes) -> None:
            events.append(("sendall", payload))

        def recv(self, size: int) -> bytes:
            events.append(("recv", size))
            return self._chunks.pop(0)

    monkeypatch.setattr(daemon_cli.socket, "socket", FakeSocket)

    response = daemon_cli._client_request(tmp_path / "broker.sock", "broker/health")

    assert response == {"result": {"ok": True}}
    assert events == [
        ("init", (daemon_cli.socket.AF_UNIX, daemon_cli.socket.SOCK_STREAM)),
        ("enter", None),
        ("connect", str(tmp_path / "broker.sock")),
        ("sendall", b'{"id": "broker/health", "method": "broker/health"}\n'),
        ("recv", 65536),
        ("recv", 65536),
        ("recv", 65536),
        ("exit", None),
    ]

@pytest.mark.error_simulation
def test_daemon_serve_forever_starts_waits_and_joins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    calls: list[str] = []

    def start() -> None:
        calls.append("start")
        daemon._stop_requested.set()

    def join(*, timeout: float) -> None:
        calls.append(f"join:{timeout}")

    monkeypatch.setattr(daemon, "start", start)
    monkeypatch.setattr(daemon, "join", join)

    daemon.serve_forever()

    assert calls == ["start", "join:5"]

@pytest.mark.error_simulation
def test_daemon_main_queries_status_and_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.daemon as daemon_module

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status and stop must not construct daemon")

    requests: list[tuple[Path, str]] = []

    def client_request(socket_path: Path, method: str) -> dict[str, object]:
        requests.append((socket_path, method))
        return {"id": method, "result": {"status": "ok"}}

    monkeypatch.setattr(daemon_module, "_client_request", client_request)
    monkeypatch.setattr(daemon_module, "BrokerDaemon", ExplodingDaemon)

    assert (
        daemon_module.main(
            [
                "status",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ]
        )
        == 0
    )
    assert (
        daemon_module.main(
            [
                "stop",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert json.loads(lines[0])["id"] == "broker/health"
    assert json.loads(lines[1])["id"] == "broker/stop"
    assert requests == [
        (tmp_path / "broker.sock", "broker/health"),
        (tmp_path / "broker.sock", "broker/stop"),
    ]

@pytest.mark.error_simulation
def test_daemon_main_serve_uses_loaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml
    import mcp_broker.daemon as daemon_module

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"root": str(tmp_path / "runtime")},
                "upstreams": {"fake": {"command": "fake", "tool_prefix": "fake"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def serve_forever(self) -> None:
            seen["served"] = True

    monkeypatch.setattr(daemon_module, "BrokerDaemon", FakeDaemon)

    assert (
        daemon_module.main(
            [
                "serve",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )

    assert seen["runtime_root"] == tmp_path / "runtime"
    assert seen["socket_path"] == tmp_path / "broker.sock"
    assert isinstance(seen["broker_config"], BrokerConfig)
    assert seen["served"] is True

@pytest.mark.error_simulation
def test_daemon_main_passes_daemon_dependencies_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    import mcp_broker.daemon_cli as daemon_cli

    seen: dict[str, object] = {}

    def fake_cli_main(argv: object, **kwargs: object) -> int:
        seen["argv"] = argv
        seen.update(kwargs)
        return 17

    monkeypatch.setattr(daemon_cli, "main", fake_cli_main)

    result = daemon_module.main(
        [
            "status",
            "--runtime-root",
            "/tmp/runtime",
            "--socket-path",
            "/tmp/broker.sock",
        ]
    )

    assert result == 17
    assert seen["argv"] == [
        "status",
        "--runtime-root",
        "/tmp/runtime",
        "--socket-path",
        "/tmp/broker.sock",
    ]
    assert seen["daemon_cls"] is daemon_module.BrokerDaemon
    assert seen["request_fn"] is daemon_module._client_request

@pytest.mark.parametrize(
    ("params", "message"),
    [
        (None, "broker config is not loaded"),
        ([], "tools/call params must be an object"),
        ({"name": 1, "arguments": {}}, "tools/call name and arguments required"),
        ({"name": "fake.echo", "arguments": []}, "tools/call name and arguments required"),
    ],
)
def test_daemon_tools_call_validation_errors(
    tmp_path: Path,
    params: object,
    message: str,
) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _empty_config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=None if params is None else config,
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params=params,
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.error == {"code": -32000 if params is None else -32602, "message": message}

def test_daemon_tools_call_maps_broker_errors(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _empty_config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params={"name": "missing.echo", "arguments": {}},
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.error == {"code": -32000, "message": "unknown tool prefix: missing"}
