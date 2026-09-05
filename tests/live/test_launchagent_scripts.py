import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "scripts" / "install-launchagent.sh"
UNINSTALL_SCRIPT = ROOT / "scripts" / "uninstall-launchagent.sh"
SYSTEMD_INSTALL_SCRIPT = ROOT / "scripts" / "install-systemd-user.sh"
SYSTEMD_UNINSTALL_SCRIPT = ROOT / "scripts" / "uninstall-systemd-user.sh"
PLIST_NAME = "com.mcp-broker.agent.plist"
SERVICE_NAME = "mcp-broker.service"


def test_install_refuses_when_broker_smoke_fails(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=37)

    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--dry-run"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert result.returncode == 37
    assert "broker-smoke failed" in result.stderr
    assert not _launchagent_path(tmp_path).exists()
    assert not (tmp_path / "runtime" / "renders" / PLIST_NAME).exists()


def test_install_dry_run_writes_launchagent_preview_only_after_smoke_passes(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)

    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--dry-run"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    preview_path = tmp_path / "runtime" / "renders" / PLIST_NAME
    assert "dry_run=true" in result.stdout
    assert "navin" not in result.stdout.lower()
    assert preview_path.is_file()
    preview_text = preview_path.read_text(encoding="utf-8")
    assert "navin" not in preview_text.lower()
    assert "<string>com.mcp-broker.agent</string>" in preview_text
    assert str(ROOT / "venv-mcp-broker" / "bin" / "python") in preview_text
    assert "<string>-m</string>" in preview_text
    assert "<string>mcp_broker.daemon</string>" in preview_text
    assert "<string>--config</string>" in preview_text
    assert env["MCP_BROKER_CONFIG"] in preview_text
    assert "<key>WorkingDirectory</key>" in preview_text
    assert "<key>AssociatedBundleIdentifiers</key>" in preview_text
    assert "<string>com.mcp-broker.agent</string>" in preview_text
    assert "<key>PYTHONPATH</key>" in preview_text
    assert "<key>PATH</key>" in preview_text
    assert f"<string>{env['PATH']}</string>" in preview_text
    assert str(ROOT / "src") in preview_text
    assert not _launchagent_path(tmp_path).exists()


def test_uninstall_dry_run_keeps_existing_launchagent_file(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)
    launchagent_path = _launchagent_path(tmp_path)
    launchagent_path.parent.mkdir(parents=True, exist_ok=True)
    launchagent_path.write_text("existing plist", encoding="utf-8")

    result = subprocess.run(
        [str(UNINSTALL_SCRIPT), "--dry-run"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert "dry_run=true" in result.stdout
    assert "navin" not in result.stdout.lower()
    assert launchagent_path.read_text(encoding="utf-8") == "existing plist"


def test_install_apply_backs_up_existing_launchagent_before_write(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)
    launchagent_path = _launchagent_path(tmp_path)
    launchagent_path.parent.mkdir(parents=True, exist_ok=True)
    launchagent_path.write_text("existing plist", encoding="utf-8")

    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--apply"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    backups = list((tmp_path / "runtime" / "backups" / "launchagent").glob(f"*.{PLIST_NAME}"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "existing plist"
    assert "backup_path=" + str(backups[0]) in result.stdout
    launchagent_text = launchagent_path.read_text(encoding="utf-8")
    assert "navin" not in result.stdout.lower()
    assert "navin" not in launchagent_text.lower()
    assert "<string>com.mcp-broker.agent</string>" in launchagent_text
    assert str(ROOT / "venv-mcp-broker" / "bin" / "python") in launchagent_text
    assert "<string>mcp_broker.daemon</string>" in launchagent_text
    assert "<string>--config</string>" in launchagent_text
    assert env["MCP_BROKER_CONFIG"] in launchagent_text
    assert _launchagent_app_bundle(tmp_path).is_dir()
    app_info = (_launchagent_app_bundle(tmp_path) / "Contents" / "Info.plist").read_text(
        encoding="utf-8"
    )
    assert "<key>CFBundleIdentifier</key>" in app_info
    assert "<string>com.mcp-broker.agent</string>" in app_info
    assert "<key>CFBundleDisplayName</key>" in app_info
    assert "<string>mcp-broker</string>" in app_info


def test_systemd_install_dry_run_writes_service_preview_after_smoke_passes(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)

    result = subprocess.run(
        [str(SYSTEMD_INSTALL_SCRIPT), "--dry-run"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    preview_path = tmp_path / "runtime" / "renders" / SERVICE_NAME
    assert "dry_run=true" in result.stdout
    assert "navin" not in result.stdout.lower()
    assert preview_path.is_file()
    preview_text = preview_path.read_text(encoding="utf-8")
    assert "navin" not in preview_text.lower()
    assert "Description=mcp-broker local MCP daemon" in preview_text
    assert "MCP_BROKER_RUNTIME_ROOT=" in preview_text
    assert "MCP_BROKER_SOCKET=" in preview_text
    assert "MCP_BROKER_CONFIG=" in preview_text
    assert f"Environment=PATH={env['PATH']}" in preview_text
    assert "mcp_broker.daemon" in preview_text
    assert env["MCP_BROKER_CONFIG"] in preview_text
    assert not _systemd_service_path(tmp_path).exists()


def test_systemd_install_apply_backs_up_existing_service_before_write(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)
    service_path = _systemd_service_path(tmp_path)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("existing service", encoding="utf-8")

    result = subprocess.run(
        [str(SYSTEMD_INSTALL_SCRIPT), "--apply"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    backups = list((tmp_path / "runtime" / "backups" / "systemd").glob(f"*.{SERVICE_NAME}"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "existing service"
    assert "backup_path=" + str(backups[0]) in result.stdout
    service_text = service_path.read_text(encoding="utf-8")
    assert "navin" not in service_text.lower()
    assert "ExecStart=" in service_text
    assert "mcp_broker.daemon" in service_text


def test_systemd_uninstall_dry_run_keeps_existing_service_file(tmp_path: Path) -> None:
    env = _env_with_controlled_make(tmp_path, exit_code=0)
    service_path = _systemd_service_path(tmp_path)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("existing service", encoding="utf-8")

    result = subprocess.run(
        [str(SYSTEMD_UNINSTALL_SCRIPT), "--dry-run"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    assert "dry_run=true" in result.stdout
    assert service_path.read_text(encoding="utf-8") == "existing service"


def _env_with_controlled_make(tmp_path: Path, *, exit_code: int) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_shim = bin_dir / "make"
    make_shim.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                'printf "%s\\n" "$*" > "$MCP_BROKER_MAKE_LOG"',
                f"exit {exit_code}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    make_shim.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
            "MCP_BROKER_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "MCP_BROKER_CONFIG": str(tmp_path / "config" / "broker.private.yaml"),
            "MCP_BROKER_MAKE_LOG": str(tmp_path / "make.log"),
            "PATH": str(bin_dir) + os.pathsep + env["PATH"],
        }
    )
    return env


def _launchagent_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / "Library" / "LaunchAgents" / PLIST_NAME


def _launchagent_app_bundle(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "launchagent" / "mcp-broker.app"


def _systemd_service_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".config" / "systemd" / "user" / SERVICE_NAME
