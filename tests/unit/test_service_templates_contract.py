import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from mcp_broker.service_templates import (
    LAUNCHAGENT_LABEL,
    SYSTEMD_SERVICE_NAME,
    WINDOWS_TASK_NAME,
    ServiceTemplateError,
    build_service_plan,
    main,
)


pytestmark = pytest.mark.unit


def test_macos_service_plan_is_dry_run_and_uses_runtime_state(tmp_path: Path) -> None:
    plan = build_service_plan(
        platform="macos",
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        config_path=tmp_path / "runtime" / "config" / "broker.yaml",
        daemon_command="/opt/mcp-broker/bin/mcp-broker-daemon",
        home_dir=tmp_path / "home",
    )

    assert plan["platform"] == "macos"
    assert plan["service_manager"] == "launchd"
    assert plan["dry_run"] is True
    assert plan["would_mutate"] is False
    assert plan["approval_required_for_apply"] is True
    assert plan["target_path"] == str(
        tmp_path / "home" / "Library" / "LaunchAgents" / f"{LAUNCHAGENT_LABEL}.plist"
    )
    assert plan["render_path"] == str(tmp_path / "runtime" / "renders" / f"{LAUNCHAGENT_LABEL}.plist")
    assert plan["command"] == (
        "/opt/mcp-broker/bin/mcp-broker-daemon serve "
        f"--runtime-root {tmp_path / 'runtime'} "
        f"--socket-path {tmp_path / 'runtime' / 'sockets' / 'broker.sock'} "
        f"--config {tmp_path / 'runtime' / 'config' / 'broker.yaml'}"
    )
    assert plan["environment"]["MCP_BROKER_RUNTIME_ROOT"] == str(tmp_path / "runtime")
    assert "navin" not in json.dumps(plan).lower()


def test_linux_service_plan_targets_systemd_user_service(tmp_path: Path) -> None:
    plan = build_service_plan(
        platform="linux",
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        config_path=tmp_path / "runtime" / "config" / "broker.yaml",
        daemon_command="mcp-broker-daemon",
        home_dir=tmp_path / "home",
    )

    assert plan["platform"] == "linux"
    assert plan["service_manager"] == "systemd-user"
    assert plan["target_path"] == str(
        tmp_path / "home" / ".config" / "systemd" / "user" / SYSTEMD_SERVICE_NAME
    )
    assert plan["render_path"] == str(tmp_path / "runtime" / "renders" / SYSTEMD_SERVICE_NAME)
    assert plan["load_command"] == f"systemctl --user enable --now {SYSTEMD_SERVICE_NAME}"
    assert plan["unload_command"] == f"systemctl --user disable --now {SYSTEMD_SERVICE_NAME}"
    assert plan["would_mutate"] is False


def test_windows_service_plan_targets_scheduled_task(tmp_path: Path) -> None:
    plan = build_service_plan(
        platform="windows",
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        config_path=tmp_path / "runtime" / "config" / "broker.yaml",
        daemon_command="mcp-broker-daemon.exe",
        home_dir=tmp_path / "home",
    )

    assert plan["platform"] == "windows"
    assert plan["service_manager"] == "windows-scheduled-task"
    assert plan["target_path"] == rf"Task Scheduler\{WINDOWS_TASK_NAME}"
    assert plan["render_path"] == str(tmp_path / "runtime" / "renders" / f"windows-task-{WINDOWS_TASK_NAME}.txt")
    assert plan["load_command"] == f"Register-ScheduledTask -TaskName {WINDOWS_TASK_NAME}"
    assert plan["unload_command"] == f"Unregister-ScheduledTask -TaskName {WINDOWS_TASK_NAME} -Confirm:$false"
    assert plan["would_mutate"] is False


def test_service_plan_rejects_unknown_platform(tmp_path: Path) -> None:
    with pytest.raises(ServiceTemplateError, match="unsupported service platform"):
        build_service_plan(
            platform="solaris",
            runtime_root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
            config_path=tmp_path / "runtime" / "config" / "broker.yaml",
            daemon_command="mcp-broker-daemon",
            home_dir=tmp_path / "home",
        )


def test_service_plan_rejects_empty_daemon_command(tmp_path: Path) -> None:
    with pytest.raises(ServiceTemplateError, match="daemon command is required"):
        build_service_plan(
            platform="linux",
            runtime_root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
            config_path=tmp_path / "runtime" / "config" / "broker.yaml",
            daemon_command=" ",
            home_dir=tmp_path / "home",
        )


def test_service_plan_rejects_malformed_daemon_command(tmp_path: Path) -> None:
    with pytest.raises(ServiceTemplateError, match="invalid daemon command"):
        build_service_plan(
            platform="linux",
            runtime_root=tmp_path / "runtime",
            socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
            config_path=tmp_path / "runtime" / "config" / "broker.yaml",
            daemon_command="'unterminated",
            home_dir=tmp_path / "home",
        )


def test_service_template_cli_writes_plan_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "--platform",
            "linux",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "runtime" / "sockets" / "broker.sock"),
            "--config",
            str(tmp_path / "runtime" / "config" / "broker.yaml"),
            "--daemon-command",
            "mcp-broker-daemon",
            "--home-dir",
            str(tmp_path / "home"),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["platform"] == "linux"
    assert payload["service_manager"] == "systemd-user"
    assert payload["service_name"] == SYSTEMD_SERVICE_NAME


def test_service_template_cli_parser_and_json_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.service_templates as service_templates

    class ParserSpy:
        description: str | None = None
        arguments: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def __init__(self, *, description: str) -> None:
            type(self).description = description
            type(self).arguments = []

        def add_argument(self, *args: object, **kwargs: object) -> None:
            type(self).arguments.append((args, kwargs))

        def parse_args(self, argv: object) -> SimpleNamespace:
            assert argv == ["ignored"]
            return SimpleNamespace(
                platform="linux",
                runtime_root=Path("runtime"),
                socket_path=Path("broker.sock"),
                config=Path("broker.yaml"),
                daemon_command="mcp-broker-daemon",
                home_dir=Path("home"),
            )

    monkeypatch.setattr(service_templates.argparse, "ArgumentParser", ParserSpy)
    monkeypatch.setattr(
        service_templates,
        "build_service_plan",
        lambda **_kwargs: {"z": 1, "a": 2},
    )

    assert service_templates.main(["ignored"]) == 0
    assert ParserSpy.description == "Render a dry-run service manager plan"
    assert ParserSpy.arguments == [
        (("--platform",), {"required": True}),
        (("--runtime-root",), {"required": True, "type": Path}),
        (("--socket-path",), {"required": True, "type": Path}),
        (("--config",), {"required": True, "type": Path}),
        (("--daemon-command",), {"required": True}),
        (("--home-dir",), {"required": True, "type": Path}),
    ]
    assert capsys.readouterr().out == '{"a": 2, "z": 1}\n'


def test_service_template_cli_reports_plan_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "--platform",
            "solaris",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "runtime" / "sockets" / "broker.sock"),
            "--config",
            str(tmp_path / "runtime" / "config" / "broker.yaml"),
            "--daemon-command",
            "mcp-broker-daemon",
            "--home-dir",
            str(tmp_path / "home"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "unsupported service platform" in captured.err


@pytest.mark.error_simulation
def test_service_template_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "service_templates",
            "--platform",
            "linux",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "runtime" / "sockets" / "broker.sock"),
            "--config",
            str(tmp_path / "runtime" / "config" / "broker.yaml"),
            "--daemon-command",
            "mcp-broker-daemon",
            "--home-dir",
            str(tmp_path / "home"),
        ],
    )

    module_name = "mcp_broker.service_templates"
    previous_module = sys.modules.pop(module_name, None)

    try:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module(module_name, run_name="__main__")
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert exit_info.value.code == 0
