import runpy
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

@pytest.mark.parametrize(
    "module_name",
    [
        "mcp_broker.client",
        "mcp_broker.cli",
        "mcp_broker.config_render",
        "mcp_broker.config_validate",
        "mcp_broker.daemon",
        "mcp_broker.deferred_acceptance",
        "mcp_broker.discovery_parity",
        "mcp_broker.doctor",
        "mcp_broker.facade_smoke",
        "mcp_broker.profile_validation",
        "mcp_broker.profile_snippet",
        "mcp_broker.project_mcp",
        "mcp_broker.runtime_reaper",
        "mcp_broker.tool_count",
    ],
)
def test_cli_module_entrypoints_delegate_to_argparse_help(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", [module_name, "--help"])
    previous_module = sys.modules.pop(module_name, None)

    try:
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module(module_name, run_name="__main__")
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert exc_info.value.code == 0

def test_top_level_cli_init_copies_packaged_example(tmp_path: Path) -> None:
    from mcp_broker.cli import main

    config_path = tmp_path / "configs" / "broker.yaml"

    assert main(["init", "--config", str(config_path)]) == 0

    rendered = config_path.read_text(encoding="utf-8")
    assert "runtime:" in rendered
    assert "upstreams:" in rendered
    assert "/Users/" not in rendered

def test_top_level_cli_init_does_not_overwrite_existing_config(tmp_path: Path) -> None:
    from mcp_broker.cli import main

    config_path = tmp_path / "broker.yaml"
    config_path.write_text("existing: true\n", encoding="utf-8")

    assert main(["init", "--config", str(config_path)]) == 0

    assert config_path.read_text(encoding="utf-8") == "existing: true\n"

def test_top_level_cli_render_dry_run_uses_config_contract(tmp_path: Path) -> None:
    from mcp_broker.cli import main

    runtime_root = tmp_path / "runtime"
    client_config = tmp_path / "client.toml"
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {runtime_root}
clients:
  generic-client:
    format: codex-toml
    config_path: {client_config}
    entry_name: mcp-broker
    command: mcp-broker-client
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )

    assert main(["render", "generic-client", "--config", str(config_path), "--dry-run"]) == 0

    rendered_path = runtime_root / "renders" / "generic-client.config.toml"
    assert rendered_path.exists()
    assert not client_config.exists()
    assert '[mcp_servers."mcp-broker"]' in rendered_path.read_text(encoding="utf-8")

def test_top_level_cli_builds_daemon_argv_from_public_options(tmp_path: Path) -> None:
    from mcp_broker.cli import daemon_argv, stdio_argv

    runtime_root = tmp_path / "runtime"
    socket_path = runtime_root / "sockets" / "broker.sock"
    config_path = tmp_path / "broker.yaml"

    assert daemon_argv(
        command="start",
        runtime_root=runtime_root,
        socket_path=socket_path,
        config_path=config_path,
    ) == [
        "serve",
        "--runtime-root",
        str(runtime_root),
        "--socket-path",
        str(socket_path),
        "--config",
        str(config_path),
    ]
    assert daemon_argv(
        command="status",
        runtime_root=runtime_root,
        socket_path=socket_path,
        config_path=None,
    ) == [
        "status",
        "--runtime-root",
        str(runtime_root),
        "--socket-path",
        str(socket_path),
    ]
    assert stdio_argv(
        runtime_root=runtime_root,
        socket_path=socket_path,
        config_path=config_path,
        profile="generic-client",
        init_if_missing=False,
    ) == [
        "--runtime-root",
        str(runtime_root),
        "--socket-path",
        str(socket_path),
        "--config",
        str(config_path),
        "--profile",
        "generic-client",
    ]

def test_top_level_cli_parser_requires_command() -> None:
    from mcp_broker.cli import build_parser

    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])

    assert exc_info.value.code == 2

def test_top_level_cli_parser_preserves_handlers_and_path_types(
    tmp_path: Path,
) -> None:
    from mcp_broker import cli

    parser = cli.build_parser()
    config_path = tmp_path / "broker.yaml"
    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / "broker.sock"

    parsed_init = parser.parse_args(["init", "--config", str(config_path)])
    parsed_start = parser.parse_args(
        [
            "start",
            "--runtime-root",
            str(runtime_root),
            "--socket-path",
            str(socket_path),
            "--config",
            str(config_path),
        ]
    )
    parsed_render = parser.parse_args(["render", "generic-client", "--config", str(config_path)])
    org_path = tmp_path / "org.yaml"
    parsed_config_compose = parser.parse_args(["config", "compose", "--org", str(org_path)])

    assert parsed_init.command == "init"
    assert parsed_init.handler is cli.handle_init
    assert parsed_init.config == config_path
    assert parsed_start.command == "start"
    assert parsed_start.handler is cli.handle_daemon
    assert parsed_start.runtime_root == runtime_root
    assert parsed_start.socket_path == socket_path
    assert parsed_start.config == config_path
    assert parsed_render.command == "render"
    assert parsed_render.handler is cli.handle_render
    assert parsed_render.client == "generic-client"
    assert parsed_render.config == config_path
    assert parsed_config_compose.command == "config"
    assert parsed_config_compose.config_command == "compose"
    assert parsed_config_compose.handler is cli.handle_config_compose
    assert parsed_config_compose.org == org_path

def test_top_level_cli_parser_help_text_is_stable() -> None:
    from mcp_broker.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()

    assert parser.description == "Initialize, run, and inspect mcp-broker"
    assert "init" in help_text
    assert "Create a private config from the public example" in help_text
    assert "start" in help_text
    assert "Start the broker daemon in the foreground" in help_text
    assert "stdio" in help_text
    assert "Run the broker daemon and stdio client in one process" in help_text
    assert "status" in help_text
    assert "Query broker daemon status" in help_text
    assert "stop" in help_text
    assert "Ask the broker daemon to stop" in help_text
    assert "render" in help_text
    assert "Render one client config" in help_text
    assert "config" in help_text
    assert "Inspect and compose broker config" in help_text
    assert "XX" not in help_text
    assert "INITIALIZE, RUN, AND INSPECT MCP-BROKER" not in help_text

def test_top_level_cli_parser_defaults_use_configured_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / "broker.sock"
    config_path = tmp_path / "broker.yaml"
    monkeypatch.setenv("MCP_BROKER_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("MCP_BROKER_SOCKET", str(socket_path))
    monkeypatch.setenv("MCP_BROKER_CONFIG", str(config_path))

    parser = cli.build_parser()
    parsed_init = parser.parse_args(["init"])
    parsed_start = parser.parse_args(["start"])
    parsed_stdio = parser.parse_args(["stdio"])
    parsed_status = parser.parse_args(["status"])
    parsed_stop = parser.parse_args(["stop"])
    parsed_render = parser.parse_args(["render", "generic-client"])

    assert parsed_init.config == config_path
    assert parsed_init.template is None
    assert parsed_init.force is False
    assert parsed_start.config == config_path
    assert parsed_start.runtime_root == runtime_root
    assert parsed_start.socket_path == socket_path
    assert parsed_stdio.config == config_path
    assert parsed_stdio.runtime_root == runtime_root
    assert parsed_stdio.socket_path == socket_path
    assert parsed_stdio.init_if_missing is False
    assert parsed_stdio.ready_attempts == 50
    assert isinstance(parsed_stdio.ready_attempts, int)
    assert parsed_status.runtime_root == runtime_root
    assert parsed_status.socket_path == socket_path
    assert parsed_stop.runtime_root == runtime_root
    assert parsed_stop.socket_path == socket_path
    assert parsed_render.config == config_path
    assert parsed_render.dry_run is True

def test_top_level_cli_stdio_parser_uses_environment_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.cli import build_parser

    monkeypatch.setenv("MCP_BROKER_PROFILE", "env-profile")
    monkeypatch.setenv("MCP_BROKER_READY_ATTEMPTS", "7")
    parser = build_parser()

    parsed = parser.parse_args(
        [
            "stdio",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
        ]
    )

    assert parsed.profile == "env-profile"
    assert parsed.ready_attempts == 7
    assert isinstance(parsed.ready_attempts, int)

def test_top_level_cli_stdio_parser_parses_ready_attempts_as_int(
    tmp_path: Path,
) -> None:
    from mcp_broker.cli import build_parser

    parser = build_parser()

    parsed = parser.parse_args(
        [
            "stdio",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
            "--ready-attempts",
            "8",
        ]
    )

    assert parsed.ready_attempts == 8
    assert isinstance(parsed.ready_attempts, int)

def test_top_level_cli_stdio_delegates_runtime_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def stdio_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    config_path = tmp_path / "broker.yaml"
    runtime_root = tmp_path / "runtime"
    socket_path = runtime_root / "broker.sock"
    monkeypatch.setattr(cli, "stdio_main", stdio_runner)

    assert (
        cli.main(
            [
                "stdio",
                "--runtime-root",
                str(runtime_root),
                "--socket-path",
                str(socket_path),
                "--config",
                str(config_path),
                "--profile",
                "generic-client",
            ]
        )
        == 0
    )
    assert calls == [
        [
            "--runtime-root",
            str(runtime_root),
            "--socket-path",
            str(socket_path),
            "--config",
            str(config_path),
            "--profile",
            "generic-client",
            "--ready-attempts",
            "50",
        ]
    ]

def test_top_level_cli_stdio_delegates_environment_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def stdio_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "stdio_main", stdio_runner)
    monkeypatch.setenv("MCP_BROKER_PROFILE", "env-profile")
    monkeypatch.setenv("MCP_BROKER_READY_ATTEMPTS", "3")

    assert (
        cli.main(
            [
                "stdio",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(tmp_path / "broker.yaml"),
            ]
        )
        == 0
    )

    assert calls == [
        [
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
            "--profile",
            "env-profile",
            "--ready-attempts",
            "3",
        ]
    ]

def test_top_level_cli_stdio_delegates_init_if_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def stdio_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "stdio_main", stdio_runner)

    assert (
        cli.main(
            [
                "stdio",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(tmp_path / "broker.yaml"),
                "--init-if-missing",
            ]
        )
        == 0
    )

    assert calls == [
        [
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
            "--init-if-missing",
            "--ready-attempts",
            "50",
        ]
    ]

def test_top_level_cli_builds_stdio_argv_without_optional_profile(tmp_path: Path) -> None:
    from mcp_broker.cli import stdio_argv

    runtime_root = tmp_path / "runtime"
    socket_path = runtime_root / "broker.sock"
    config_path = tmp_path / "broker.yaml"

    assert stdio_argv(
        runtime_root=runtime_root,
        socket_path=socket_path,
        config_path=config_path,
        profile=None,
        init_if_missing=True,
    ) == [
        "--runtime-root",
        str(runtime_root),
        "--socket-path",
        str(socket_path),
        "--config",
        str(config_path),
        "--init-if-missing",
    ]

def test_top_level_cli_stdio_argv_default_does_not_enable_init(tmp_path: Path) -> None:
    import inspect

    from mcp_broker.cli import stdio_argv

    runtime_root = tmp_path / "runtime"
    socket_path = runtime_root / "broker.sock"
    config_path = tmp_path / "broker.yaml"

    assert inspect.signature(stdio_argv).parameters["init_if_missing"].default is inspect.Parameter.empty
    assert stdio_argv(
        runtime_root=runtime_root,
        socket_path=socket_path,
        config_path=config_path,
        profile=None,
        init_if_missing=False,
    ) == [
        "--runtime-root",
        str(runtime_root),
        "--socket-path",
        str(socket_path),
        "--config",
        str(config_path),
    ]

def test_top_level_cli_stdio_runs_daemon_and_client_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    config_path = _stdio_config(tmp_path)
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
            ]
        )
        == 0
    )
