import argparse
import json
from pathlib import Path
import pytest
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
def _config(tmp_path: Path):
    from mcp_broker.config import BrokerConfig

    return BrokerConfig.from_file(_config_file(tmp_path))
def _config_render_parser() -> argparse.ArgumentParser:
    from mcp_broker.config_render import _add_subcommands

    parser = argparse.ArgumentParser(description="contract")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_subcommands(subparsers)
    return parser
def _config_file(tmp_path: Path, client_extra: dict[str, object] | None = None) -> Path:
    import yaml

    runtime_root = tmp_path / "runtime"
    path = tmp_path / "broker.yaml"
    codex_client = {
        "format": "codex-toml",
        "config_path": str(tmp_path / "codex.toml"),
        "entry_name": "mcp-broker",
        "command": "mcp-broker-client",
    }
    if client_extra:
        codex_client.update(client_extra)
    path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(runtime_root / "sockets" / "broker.sock"),
                },
                "clients": {
                    "codex": codex_client
                },
                "upstreams": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path

def test_mcp_settings_json_render_recovers_from_invalid_existing_objects(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    list_target_path = tmp_path / "list-settings.json"
    list_target_path.write_text("[]\n", encoding="utf-8")
    bad_mcp_target_path = tmp_path / "bad-mcp-settings.json"
    bad_mcp_target_path.write_text(
        json.dumps({"mcp": [], "mcpServers": {"old": {"command": "old"}}}),
        encoding="utf-8",
    )
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(runtime_root / "sockets" / "broker.sock"),
                },
                "clients": {
                    "list-client": {
                        "format": "mcp-settings-json",
                        "config_path": str(list_target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "mcp_allowed_servers": ["mcp-broker"],
                    },
                    "bad-mcp-client": {
                        "format": "mcp-settings-json",
                        "config_path": str(bad_mcp_target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "mcp_allowed_servers": ["mcp-broker"],
                    },
                },
                "upstreams": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    list_result = render_client_config(
        BrokerConfig.from_file(config_path),
        client_name="list-client",
        dry_run=True,
    )
    bad_mcp_result = render_client_config(
        BrokerConfig.from_file(config_path),
        client_name="bad-mcp-client",
        dry_run=True,
    )

    assert json.loads(list_result.rendered_path.read_text(encoding="utf-8"))["mcp"] == {
        "allowed": ["mcp-broker"],
    }
    assert json.loads(bad_mcp_result.rendered_path.read_text(encoding="utf-8"))["mcp"] == {
        "allowed": ["mcp-broker"],
    }

def test_codex_render_uses_home_relative_socket_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "broker.yaml"
    target_path = tmp_path / "codex.toml"
    config_path.write_text(
        f"""
runtime:
  root: $HOME/mcp/mcp-broker
clients:
  codex:
    format: codex-toml
    config_path: {target_path}
    entry_name: mcp-broker
    command: mcp-broker-client
    args:
      - --socket-path
      - "{{runtime.socket_path}}"
      - --profile
      - codex
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )

    result = render_client_config(BrokerConfig.from_file(config_path), client_name="codex", dry_run=True)

    rendered = result.rendered_path.read_text(encoding="utf-8")
    assert f"{home}/mcp/mcp-broker/sockets/broker.sock" not in rendered
    assert 'args = ["--socket-path", "$HOME/mcp/mcp-broker/sockets/broker.sock", "--profile", "codex"]' in rendered

def test_portable_client_arg_preserves_paths_when_home_is_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_broker.config_render import _portable_client_arg

    monkeypatch.setenv("HOME", "/")

    assert _portable_client_arg("/runtime/sockets/broker.sock") == "/runtime/sockets/broker.sock"

def test_portable_client_arg_can_render_home_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config_render import _portable_client_arg

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert _portable_client_arg(str(home)) == "$HOME"

def test_apply_render_can_backup_missing_target_as_empty_file(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)

    result = render_client_config(
        config,
        client_name="codex",
        dry_run=False,
        backup_label="20260524T010101Z",
    )

    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == ""

def test_apply_render_can_target_an_override_config_path(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    configured_path = tmp_path / "configured-client.toml"
    override_path = tmp_path / "configs" / "config.project-under-test.toml"
    configured_path.write_text("configured = true\n", encoding="utf-8")
    override_path.parent.mkdir()
    override_path.write_text("project = true\n[mcp_servers.read-store]\ncommand = \"read-store\"\n", encoding="utf-8")
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(runtime_root / "sockets" / "broker.sock"),
                },
                "clients": {
                    "toml-client": {
                        "format": "codex-toml",
                        "config_path": str(configured_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                    },
                },
                "upstreams": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = render_client_config(
        BrokerConfig.from_file(config_path),
        client_name="toml-client",
        dry_run=False,
        backup_label="20260524T060606Z",
        target_path=override_path,
    )

    assert result.target_path == override_path
    assert configured_path.read_text(encoding="utf-8") == "configured = true\n"
    assert override_path.read_text(encoding="utf-8") == (
        "project = true\n\n"
        '[mcp_servers."mcp-broker"]\n'
        'command = "mcp-broker-client"\n'
        f'args = ["--socket-path", "{runtime_root}/sockets/broker.sock"]\n'
    )
    assert (
        result.backup_path
        == runtime_root / "backups" / "toml-client" / "20260524T060606Z.config.project-under-test.toml"
    )
    assert result.backup_path.read_text(encoding="utf-8") == (
        "project = true\n[mcp_servers.read-store]\ncommand = \"read-store\"\n"
    )

def test_codex_render_drops_legacy_synced_mcp_comment_blocks(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text(
        'approval_policy = "never"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "# === MCP servers (synced from ~/mcp/servers.json + project .mcp.json files) ===\n"
        "# Last synced: 2026-05-24 12:47\n"
        "# DO NOT EDIT - modify source files instead, then run sync-to-codex.sh\n"
        "# Secrets resolved at runtime via env-mcp.sh wrapper (no plaintext in this file)\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "# =======================================\n"
        "#   GLOBAL SERVERS (all projects)\n"
        "# =======================================\n"
        "\n"
        "## Enhanced read-store-mcp (local fork runtime, project-scoped SQLite, no runtime npx)\n"
        "\n"
        '[mcp_servers."read-store"]\n'
        'command = "read-store"\n',
        encoding="utf-8",
    )

    render_client_config(config, client_name="codex", dry_run=False)

    assert target_path.read_text(encoding="utf-8") == (
        'approval_policy = "never"\n'
        "\n"
        '[mcp_servers."mcp-broker"]\n'
        'command = "mcp-broker-client"\n'
        f'args = ["--socket-path", "{tmp_path}/runtime/sockets/broker.sock"]\n'
    )

def test_codex_render_drops_trailing_separator_before_broker_entry(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text(
        'approval_policy = "never"\n\n# -----------------------------------------------------------------------------\n\n',
        encoding="utf-8",
    )

    render_client_config(config, client_name="codex", dry_run=False)

    assert target_path.read_text(encoding="utf-8") == (
        'approval_policy = "never"\n'
        "\n"
        '[mcp_servers."mcp-broker"]\n'
        'command = "mcp-broker-client"\n'
        f'args = ["--socket-path", "{tmp_path}/runtime/sockets/broker.sock"]\n'
    )

def test_codex_render_preserves_non_legacy_separator_blocks(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text(
        'approval_policy = "never"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n",
        encoding="utf-8",
    )

    render_client_config(config, client_name="codex", dry_run=False)

    assert target_path.read_text(encoding="utf-8") == (
        'approval_policy = "never"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        '[mcp_servers."mcp-broker"]\n'
        'command = "mcp-broker-client"\n'
        f'args = ["--socket-path", "{tmp_path}/runtime/sockets/broker.sock"]\n'
    )

def test_backup_client_config_copies_target_and_related_paths_without_rendering(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import backup_client_config

    config_path = _config_file(
        tmp_path,
        client_extra={
            "backup_paths": [str(tmp_path / "claude-settings.json"), str(tmp_path / "missing.json")]
        },
    )
    target_path = tmp_path / "codex.toml"
    settings_path = tmp_path / "claude-settings.json"
    target_path.write_text('approval_policy = "never"\n', encoding="utf-8")
    settings_path.write_text('{"theme":"dark"}\n', encoding="utf-8")

    result = backup_client_config(
        BrokerConfig.from_file(config_path),
        client_name="codex",
        backup_label="20260524T020202Z",
    )

    assert result.client_name == "codex"
    assert target_path.read_text(encoding="utf-8") == 'approval_policy = "never"\n'
    assert sorted(path.name for path in result.backup_paths) == [
        "20260524T020202Z.claude-settings.json",
        "20260524T020202Z.codex.toml",
        "20260524T020202Z.missing.json",
    ]
    assert (tmp_path / "runtime" / "backups" / "codex" / "20260524T020202Z.codex.toml").read_text(
        encoding="utf-8"
    ) == 'approval_policy = "never"\n'
    assert (
        tmp_path / "runtime" / "backups" / "codex" / "20260524T020202Z.claude-settings.json"
    ).read_text(encoding="utf-8") == '{"theme":"dark"}\n'
    assert (
        tmp_path / "runtime" / "backups" / "codex" / "20260524T020202Z.missing.json"
    ).read_text(encoding="utf-8") == ""
    assert not (tmp_path / "runtime" / "renders" / "codex.config.toml").exists()

def test_rollback_rejects_missing_backups(tmp_path: Path) -> None:
    from mcp_broker.config_render import rollback_client_config

    config = _config(tmp_path)

    with pytest.raises(ValueError, match="no backups found for client: codex"):
        rollback_client_config(config, client_name="codex")

def test_rollback_creates_nested_target_parent(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import rollback_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "clients" / "nested" / "codex.toml"
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(runtime_root / "sockets" / "broker.sock"),
                },
                "clients": {
                    "codex": {
                        "format": "codex-toml",
                        "config_path": str(target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                    },
                },
                "upstreams": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    backup_path = runtime_root / "backups" / "codex" / "20260525T141414Z.codex.toml"
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text("restored = true\n", encoding="utf-8")

    result = rollback_client_config(BrokerConfig.from_file(config_path), client_name="codex")

    assert result.target_path == target_path
    assert result.restored_path == backup_path
    assert target_path.read_text(encoding="utf-8") == "restored = true\n"

def test_config_render_cli_outputs_json_for_dry_run_and_rollback(tmp_path: Path) -> None:
    from mcp_broker.config_render import main

    config_path = _config_file(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text("original = true\n", encoding="utf-8")

    assert main(["render", "--config", str(config_path), "--client", "codex"]) == 0
    assert target_path.read_text(encoding="utf-8") == "original = true\n"
    assert main(["render", "--config", str(config_path), "--client", "codex", "--apply"]) == 0
    assert '[mcp_servers."mcp-broker"]' in target_path.read_text(encoding="utf-8")
    assert main(["rollback", "--config", str(config_path), "--client", "codex"]) == 0
    assert target_path.read_text(encoding="utf-8") == "original = true\n"

def test_config_render_cli_target_path_wiring_outputs_one_json_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_render import main

    config_path = _config_file(tmp_path)
    configured_path = tmp_path / "codex.toml"
    target_path = tmp_path / "project-config.toml"
    configured_path.write_text("configured = true\n", encoding="utf-8")
    target_path.write_text("project = true\n", encoding="utf-8")

    assert (
        main(
            [
                "render",
                "--config",
                str(config_path),
                "--client",
                "codex",
                "--apply",
                "--target-path",
                str(target_path),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert output.endswith("\n")
    assert output.count("\n") == 1
    parsed = json.loads(output)
    assert parsed["client_name"] == "codex"
    assert parsed["target_path"] == str(target_path)
    assert Path(parsed["backup_path"]).parent == tmp_path / "runtime" / "backups" / "codex"
    assert Path(parsed["backup_path"]).name.endswith(f".{target_path.name}")
    assert parsed["dry_run"] is False
    assert configured_path.read_text(encoding="utf-8") == "configured = true\n"
    assert '[mcp_servers."mcp-broker"]' in target_path.read_text(encoding="utf-8")

def test_config_render_cli_outputs_json_for_backup_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.config_render import main

    config_path = _config_file(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text("original = true\n", encoding="utf-8")

    assert main(
        ["backup", "--config", str(config_path), "--client", "codex", "--label", "20260524T030303Z"]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["client_name"] == "codex"
    assert output["backup_paths"] == [
        str(tmp_path / "runtime" / "backups" / "codex" / "20260524T030303Z.codex.toml")
    ]
    assert target_path.read_text(encoding="utf-8") == "original = true\n"
