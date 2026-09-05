import os
import sys
from io import BytesIO
from pathlib import Path
import pytest
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
class _BinaryConsole:
    def __init__(self, payload: bytes = b"") -> None:
        self.buffer = BytesIO(payload)
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        return None
def _stdio_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "broker.yaml"
    runtime_root = tmp_path / "runtime"
    config_path.write_text(
        f"""
runtime:
  root: {runtime_root}
  socket_path: {_stdio_socket(tmp_path)}
  state_dir: {runtime_root / "state"}
broker:
  tool_namespace_separator: "."
profiles:
  generic-client:
    max_tools: 200
    compact_tools_enabled: false
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )
    return config_path
def _stdio_socket(tmp_path: Path) -> Path:
    return Path("/tmp") / f"mcp-broker-cli-{os.getpid()}-{tmp_path.name}.sock"

def test_top_level_cli_stdio_requires_runtime_socket_and_config() -> None:
    from mcp_broker import cli

    missing_arg_cases = [
        ["--socket-path", "broker.sock", "--config", "broker.yaml"],
        ["--runtime-root", "runtime", "--config", "broker.yaml"],
        ["--runtime-root", "runtime", "--socket-path", "broker.sock"],
    ]

    for argv in missing_arg_cases:
        with pytest.raises(SystemExit) as exc_info:
            cli.stdio_main(argv)
        assert exc_info.value.code == 2

def test_top_level_cli_stdio_passes_config_socket_profile_and_buffers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
    socket_path = _stdio_socket(tmp_path)
    seen: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, *, runtime_root: Path, socket_path: Path, broker_config: object) -> None:
            seen["runtime_root"] = runtime_root
            seen["daemon_socket_path"] = socket_path
            seen["broker_config"] = broker_config

        def start(self) -> None:
            seen["started"] = True

        def stop(self) -> None:
            seen["stopped"] = True

    class FakeClientShim:
        def __init__(self, *, socket_path: Path, profile: str | None) -> None:
            seen["client_socket_path"] = socket_path
            seen["profile"] = profile

        def run_stdio(self, stdin: object, stdout: object) -> None:
            seen["stdin"] = stdin
            seen["stdout"] = stdout

    stdin = _BinaryConsole(b'{"jsonrpc":"2.0","id":"unit"}\n')
    stdout = _BinaryConsole()
    monkeypatch.setattr(cli, "BrokerDaemon", FakeDaemon)
    monkeypatch.setattr(cli, "ClientShim", FakeClientShim)
    monkeypatch.setattr(cli, "_wait_for_socket", lambda socket_path, attempts: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(socket_path),
                "--config",
                str(config_path),
                "--profile",
                "generic-client",
                "--ready-attempts",
                "3",
            ]
        )
        == 0
    )

    assert seen["runtime_root"] == tmp_path / "runtime"
    assert seen["daemon_socket_path"] == socket_path
    assert seen["client_socket_path"] == socket_path
    assert seen["profile"] == "generic-client"
    assert seen["stdin"] is stdin.buffer
    assert seen["stdout"] is stdout.buffer
    assert seen["started"] is True
    assert seen["stopped"] is True
    assert seen["broker_config"].profiles["generic-client"].max_tools == 200

def test_top_level_cli_stdio_uses_existing_daemon_when_init_if_missing_sees_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
    socket_path = _stdio_socket(tmp_path)
    seen: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, *, runtime_root: Path, socket_path: Path, broker_config: object) -> None:
            seen["runtime_root"] = runtime_root
            seen["daemon_socket_path"] = socket_path
            seen["broker_config"] = broker_config

        def start(self) -> None:
            raise cli.BrokerDaemonError("broker daemon already running: pid 123")

        def stop(self) -> None:
            raise AssertionError("stdio must not stop a daemon it did not start")

    class FakeClientShim:
        def __init__(self, *, socket_path: Path, profile: str | None) -> None:
            seen["client_socket_path"] = socket_path
            seen["profile"] = profile

        def run_stdio(self, stdin: object, stdout: object) -> None:
            seen["stdin"] = stdin
            seen["stdout"] = stdout

    stdin = _BinaryConsole(b"")
    stdout = _BinaryConsole()
    monkeypatch.setattr(cli, "BrokerDaemon", FakeDaemon)
    monkeypatch.setattr(cli, "ClientShim", FakeClientShim)
    monkeypatch.setattr(cli, "_wait_for_socket", lambda socket_path, attempts: True)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(socket_path),
                "--config",
                str(config_path),
                "--profile",
                "generic-client",
                "--init-if-missing",
            ]
        )
        == 0
    )

    assert seen["daemon_socket_path"] == socket_path
    assert seen["client_socket_path"] == socket_path
    assert seen["profile"] == "generic-client"
    assert seen["stdin"] is stdin.buffer
    assert seen["stdout"] is stdout.buffer

def test_top_level_cli_stdio_reports_unexpected_daemon_start_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
    socket_path = _stdio_socket(tmp_path)

    class FakeDaemon:
        def __init__(self, *, runtime_root: Path, socket_path: Path, broker_config: object) -> None:
            self.stop_called = False

        def start(self) -> None:
            raise cli.BrokerDaemonError("runtime lock is corrupt")

        def stop(self) -> None:
            raise AssertionError("stdio must not stop a daemon that failed to start")

    class UnexpectedClientShim:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("client shim must not run when daemon start fails")

    monkeypatch.setattr(cli, "BrokerDaemon", FakeDaemon)
    monkeypatch.setattr(cli, "ClientShim", UnexpectedClientShim)
    monkeypatch.setattr(
        cli,
        "_wait_for_socket",
        lambda _socket_path, _attempts: (_ for _ in ()).throw(
            AssertionError("socket wait must not run when daemon start fails")
        ),
    )

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(socket_path),
                "--config",
                str(config_path),
                "--profile",
                "generic-client",
                "--init-if-missing",
            ]
        )
        == 1
    )

    assert capsys.readouterr().err == "runtime lock is corrupt\n"

def test_top_level_cli_stdio_init_error_stops_before_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[tuple[object, ...]] = []

    def initialize_config(config_path: Path, *, template_path: Path, force: bool) -> int:
        calls.append((config_path, template_path, force))
        return 17

    class UnexpectedDaemon:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("daemon must not start when init fails")

    template_path = tmp_path / "template.yaml"
    config_path = tmp_path / "generated" / "broker.yaml"
    monkeypatch.setattr(cli, "initialize_config", initialize_config)
    monkeypatch.setattr(cli, "default_template_path", lambda: template_path)
    monkeypatch.setattr(cli, "BrokerDaemon", UnexpectedDaemon)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(config_path),
                "--init-if-missing",
            ]
        )
        == 17
    )

    assert calls == [(config_path, template_path, False)]

def test_top_level_cli_stdio_initializes_missing_config_from_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    template_path = _stdio_config(tmp_path)
    config_path = tmp_path / "generated" / "broker.yaml"
    monkeypatch.setattr(cli, "default_template_path", lambda: template_path)
    monkeypatch.setattr(sys, "stdin", _BinaryConsole(b""))
    monkeypatch.setattr(sys, "stdout", _BinaryConsole())

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(_stdio_socket(tmp_path)),
                "--config",
                str(config_path),
                "--profile",
                "generic-client",
                "--init-if-missing",
            ]
        )
        == 0
    )

    assert config_path.exists()

def test_top_level_cli_stdio_reports_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker import cli

    config_path = tmp_path / "missing.yaml"

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(_stdio_socket(tmp_path)),
                "--config",
                str(config_path),
            ]
        )
        == 1
    )

    assert f"missing config: {config_path}" in capsys.readouterr().err

def test_top_level_cli_stdio_returns_template_initialization_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli

    missing_template = tmp_path / "missing-template.yaml"
    config_path = tmp_path / "generated" / "broker.yaml"
    monkeypatch.setattr(cli, "default_template_path", lambda: missing_template)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(_stdio_socket(tmp_path)),
                "--config",
                str(config_path),
                "--init-if-missing",
            ]
        )
        == 1
    )

    assert f"missing config template: {missing_template}" in capsys.readouterr().err

def test_top_level_cli_stdio_init_success_must_leave_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    def initialize_config(config_path: Path, *, template_path: Path, force: bool) -> int:
        return 0

    class UnexpectedDaemon:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("daemon must not start without a config file")

    monkeypatch.setattr(cli, "initialize_config", initialize_config)
    monkeypatch.setattr(cli, "default_template_path", lambda: tmp_path / "template.yaml")
    monkeypatch.setattr(cli, "BrokerDaemon", UnexpectedDaemon)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(tmp_path / "generated" / "broker.yaml"),
                "--init-if-missing",
            ]
        )
        == 1
    )

def test_top_level_cli_stdio_reports_socket_readiness_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
    socket_path = _stdio_socket(tmp_path)
    monkeypatch.setattr(cli, "_wait_for_socket", lambda socket_path, attempts: False)

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(socket_path),
                "--config",
                str(config_path),
            ]
        )
        == 1
    )

    assert f"broker socket did not become ready: {socket_path}" in capsys.readouterr().err

def test_top_level_cli_stdio_reports_client_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli
    from mcp_broker.client import ClientShimError

    config_path = _stdio_config(tmp_path)

    def fail_stdio(*args: object, **kwargs: object) -> None:
        raise ClientShimError("client failed")

    monkeypatch.setattr(cli.ClientShim, "run_stdio", fail_stdio)
    monkeypatch.setattr(sys, "stdin", _BinaryConsole(b""))
    monkeypatch.setattr(sys, "stdout", _BinaryConsole())

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(_stdio_socket(tmp_path)),
                "--config",
                str(config_path),
            ]
        )
        == 1
    )

    assert "client failed" in capsys.readouterr().err

def test_top_level_cli_stdio_help_text_is_stable(capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker import cli

    with pytest.raises(SystemExit) as exc_info:
        cli.stdio_main(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Run mcp-broker as a one-process stdio server" in help_text
    assert "--ready-attempts" in help_text
    assert "XX" not in help_text
    assert "RUN MCP-BROKER AS A ONE-PROCESS STDIO SERVER" not in help_text

def test_top_level_cli_stdio_passes_ready_attempts_to_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
    seen: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class FakeClientShim:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run_stdio(self, _stdin: object, _stdout: object) -> None:
            pass

    def wait_for_socket(socket_path: Path, *, attempts: int) -> bool:
        seen["attempts"] = attempts
        return True

    monkeypatch.setattr(cli, "BrokerDaemon", FakeDaemon)
    monkeypatch.setattr(cli, "ClientShim", FakeClientShim)
    monkeypatch.setattr(cli, "_wait_for_socket", wait_for_socket)
    monkeypatch.setattr(sys, "stdin", _BinaryConsole(b""))
    monkeypatch.setattr(sys, "stdout", _BinaryConsole())

    assert (
        cli.stdio_main(
            [
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(_stdio_socket(tmp_path)),
                "--config",
                str(config_path),
                "--ready-attempts",
                "4",
            ]
        )
        == 0
    )

    assert seen["attempts"] == 4
    assert isinstance(seen["attempts"], int)
