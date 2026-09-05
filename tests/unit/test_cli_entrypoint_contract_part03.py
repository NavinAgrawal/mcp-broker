import os
import sys
from argparse import Namespace
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

def test_top_level_cli_stdio_uses_default_ready_attempts_for_waiter(
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
            ]
        )
        == 0
    )

    assert seen["attempts"] == 50

def test_top_level_cli_wait_for_socket_observes_existing_and_missing_paths(tmp_path: Path) -> None:
    from mcp_broker.cli import _wait_for_socket

    socket_path = tmp_path / "broker.sock"

    assert not _wait_for_socket(socket_path, attempts=1)
    socket_path.touch()
    assert _wait_for_socket(socket_path, attempts=1)

def test_top_level_cli_wait_for_socket_uses_injected_waiter(tmp_path: Path) -> None:
    from mcp_broker.cli import _wait_for_socket

    socket_path = tmp_path / "broker.sock"
    waits: list[float] = []

    assert not _wait_for_socket(socket_path, attempts=3, wait=waits.append)

    assert waits == [0.1, 0.1]

def test_top_level_cli_default_paths_can_use_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.cli import default_config_path, default_runtime_root, default_socket_path

    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / "custom.sock"
    config_path = tmp_path / "broker.yaml"
    monkeypatch.setenv("MCP_BROKER_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("MCP_BROKER_SOCKET", str(socket_path))
    monkeypatch.setenv("MCP_BROKER_CONFIG", str(config_path))

    assert default_runtime_root() == runtime_root
    assert default_socket_path() == socket_path
    assert default_config_path() == config_path

def test_top_level_cli_default_paths_use_home_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.cli import default_config_path, default_runtime_root, default_socket_path

    monkeypatch.delenv("MCP_BROKER_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("MCP_BROKER_SOCKET", raising=False)
    monkeypatch.delenv("MCP_BROKER_CONFIG", raising=False)

    runtime_root = Path.home() / "mcp" / "mcp-broker"

    assert default_runtime_root() == runtime_root
    assert default_socket_path() == runtime_root / "sockets" / "broker.sock"
    assert default_config_path() == runtime_root / "config" / "broker.yaml"

def test_top_level_cli_template_candidates_use_installed_fallback(tmp_path: Path) -> None:
    from mcp_broker.cli import template_path_from_candidates

    source_template = tmp_path / "missing" / "broker.example.yaml"
    installed_template = tmp_path / "share" / "broker.example.yaml"
    installed_template.parent.mkdir(parents=True)
    installed_template.write_text("runtime: {}\n", encoding="utf-8")

    assert template_path_from_candidates(source_template, installed_template) == installed_template

def test_top_level_cli_template_candidates_return_last_missing_candidate(tmp_path: Path) -> None:
    from mcp_broker.cli import template_path_from_candidates

    source_template = tmp_path / "missing-source" / "broker.example.yaml"
    current_template = tmp_path / "missing-current" / "broker.example.yaml"
    installed_template = tmp_path / "missing-installed" / "broker.example.yaml"

    assert template_path_from_candidates(source_template, current_template, installed_template) == installed_template

def test_top_level_cli_default_template_path_prefers_source_tree_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    source_root = tmp_path / "source"
    source_template = source_root / "config" / "broker.example.yaml"
    current_template = tmp_path / "cwd" / "config" / "broker.example.yaml"
    installed_template = tmp_path / "venv" / "share" / "mcp-broker" / "config" / "broker.example.yaml"
    source_template.parent.mkdir(parents=True)
    current_template.parent.mkdir(parents=True)
    installed_template.parent.mkdir(parents=True)
    source_template.write_text("source: true\n", encoding="utf-8")
    current_template.write_text("current: true\n", encoding="utf-8")
    installed_template.write_text("installed: true\n", encoding="utf-8")

    monkeypatch.chdir(current_template.parents[1])
    monkeypatch.setattr(cli, "__file__", str(source_root / "src" / "mcp_broker" / "cli.py"))
    monkeypatch.setattr(sys, "prefix", str(installed_template.parents[3]))

    assert cli.default_template_path() == source_template

def test_top_level_cli_default_template_path_uses_installed_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    missing_source_root = tmp_path / "source"
    cwd = tmp_path / "cwd"
    installed_template = tmp_path / "venv" / "share" / "mcp-broker" / "config" / "broker.example.yaml"
    cwd.mkdir()
    installed_template.parent.mkdir(parents=True)
    installed_template.write_text("installed: true\n", encoding="utf-8")

    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "__file__", str(missing_source_root / "src" / "mcp_broker" / "cli.py"))
    monkeypatch.setattr(sys, "prefix", str(installed_template.parents[3]))

    assert cli.default_template_path() == installed_template

def test_top_level_cli_default_template_path_uses_current_repo_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    repo_template = tmp_path / "config" / "broker.example.yaml"
    repo_template.parent.mkdir(parents=True)
    repo_template.write_text("runtime: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "__file__",
        str(tmp_path / "venv" / "site-packages" / "mcp_broker" / "cli.py"),
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))

    assert cli.default_template_path() == repo_template

def test_top_level_cli_init_force_overwrites_existing_config(tmp_path: Path) -> None:
    from mcp_broker.cli import main

    template_path = tmp_path / "template.yaml"
    config_path = tmp_path / "nested" / "configs" / "broker.yaml"
    template_path.write_text("runtime: {}\n", encoding="utf-8")
    config_path.parent.mkdir(parents=True)
    config_path.write_text("existing: true\n", encoding="utf-8")

    assert main(["init", "--config", str(config_path), "--template", str(template_path), "--force"]) == 0

    assert config_path.read_text(encoding="utf-8") == "runtime: {}\n"

def test_top_level_cli_initialize_config_creates_nested_parent_dirs(tmp_path: Path) -> None:
    from mcp_broker.cli import initialize_config

    template_path = tmp_path / "template.yaml"
    config_path = tmp_path / "a" / "b" / "broker.yaml"
    template_path.write_text("runtime: {}\n", encoding="utf-8")

    assert initialize_config(config_path, template_path=template_path, force=False) == 0

    assert config_path.read_text(encoding="utf-8") == "runtime: {}\n"

def test_top_level_cli_init_reports_missing_template(tmp_path: Path) -> None:
    from mcp_broker.cli import main

    assert (
        main(
            [
                "init",
                "--config",
                str(tmp_path / "broker.yaml"),
                "--template",
                str(tmp_path / "missing.yaml"),
            ]
        )
        == 1
    )

def test_top_level_cli_daemon_handler_delegates_constructed_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def daemon_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "daemon_main", daemon_runner)

    assert cli.main(["status", "--runtime-root", str(tmp_path), "--socket-path", str(tmp_path / "sock")]) == 0

    assert calls == [
        [
            "status",
            "--runtime-root",
            str(tmp_path),
            "--socket-path",
            str(tmp_path / "sock"),
        ]
    ]

def test_top_level_cli_daemon_handler_start_passes_config_but_status_and_stop_do_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def daemon_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "daemon_main", daemon_runner)

    assert (
        cli.main(
            [
                "start",
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
    assert (
        cli.main(
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
        cli.main(
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

    assert calls == [
        [
            "serve",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
        ],
        [
            "status",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
        ],
        [
            "stop",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "broker.sock"),
        ],
    ]

def test_top_level_cli_daemon_argv_ignores_config_for_non_serve_commands(tmp_path: Path) -> None:
    from mcp_broker.cli import daemon_argv

    assert daemon_argv(
        command="status",
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "broker.sock",
        config_path=tmp_path / "broker.yaml",
    ) == [
        "status",
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--socket-path",
        str(tmp_path / "broker.sock"),
    ]

def test_top_level_cli_render_apply_delegates_target_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []

    def render_runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    config_path = tmp_path / "broker.yaml"
    target_path = tmp_path / "client.toml"
    monkeypatch.setattr(cli, "config_render_main", render_runner)

    assert (
        cli.main(
            [
                "render",
                "generic-client",
                "--config",
                str(config_path),
                "--apply",
                "--target-path",
                str(target_path),
            ]
        )
        == 0
    )

    assert calls == [
        [
            "render",
            "--config",
            str(config_path),
            "--client",
            "generic-client",
            "--apply",
            "--target-path",
            str(target_path),
        ]
    ]

def test_top_level_cli_bundle_validate_delegates_expanded_bundle_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "bundle_loader_main", lambda argv: calls.append(argv) or 0)

    assert cli.handle_bundle_validate(Namespace(bundle=tmp_path / "bundle.json")) == 0

    assert calls == [["--bundle", str(tmp_path / "bundle.json")]]

def test_top_level_cli_render_delegates_apply_and_target_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "config_render_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_render(
            Namespace(
                config=tmp_path / "broker.yaml",
                client="codex",
                apply=True,
                target_path=tmp_path / "settings.toml",
            )
        )
        == 0
    )

    assert calls == [
        [
            "render",
            "--config",
            str(tmp_path / "broker.yaml"),
            "--client",
            "codex",
            "--apply",
            "--target-path",
            str(tmp_path / "settings.toml"),
        ]
    ]

def test_top_level_cli_config_compose_delegates_all_layer_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "config_layers_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_config_compose(
            Namespace(
                org=tmp_path / "org.yaml",
                team=tmp_path / "team.yaml",
                addon=[tmp_path / "addon-a.yaml", tmp_path / "addon-b.yaml"],
                user=tmp_path / "user.yaml",
            )
        )
        == 0
    )

    assert calls == [
        [
            "--org",
            str(tmp_path / "org.yaml"),
            "--team",
            str(tmp_path / "team.yaml"),
            "--addon",
            str(tmp_path / "addon-a.yaml"),
            "--addon",
            str(tmp_path / "addon-b.yaml"),
            "--user",
            str(tmp_path / "user.yaml"),
        ]
    ]

def test_top_level_cli_config_compose_delegates_empty_layer_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "config_layers_main", lambda argv: calls.append(argv) or 0)

    assert cli.handle_config_compose(Namespace(org=None, team=None, addon=[], user=None)) == 0

    assert calls == [[]]
