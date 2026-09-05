import argparse
from dataclasses import replace
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

def test_render_rejects_unknown_client(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)

    with pytest.raises(ValueError, match="unknown client config: missing"):
        render_client_config(config, client_name="missing", dry_run=True)

def test_render_rejects_unsupported_client_format(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    broken = BrokerConfig(
        runtime=config.runtime,
        broker=config.broker,
        upstreams=config.upstreams,
        clients={"codex": replace(config.clients["codex"], format="ini")},
    )

    with pytest.raises(ValueError, match="unsupported client config format: ini"):
        render_client_config(broken, client_name="codex", dry_run=True)

def test_config_render_subcommands_parse_exact_command_contract() -> None:
    from mcp_broker.config_render import _add_subcommands

    parser = _config_render_parser()

    backup = parser.parse_args(["backup", "--config", "cfg.yaml", "--client", "codex", "--label", "L"])
    assert vars(backup) == {
        "command": "backup",
        "config": "cfg.yaml",
        "client": "codex",
        "label": "L",
    }

    dry_render = parser.parse_args(["render", "--config", "cfg.yaml", "--client", "codex"])
    assert vars(dry_render) == {
        "command": "render",
        "config": "cfg.yaml",
        "client": "codex",
        "apply": False,
        "target_path": None,
    }

    apply_render = parser.parse_args(
        [
            "render",
            "--config",
            "cfg.yaml",
            "--client",
            "codex",
            "--apply",
            "--target-path",
            "/tmp/project.toml",
        ]
    )
    assert vars(apply_render) == {
        "command": "render",
        "config": "cfg.yaml",
        "client": "codex",
        "apply": True,
        "target_path": "/tmp/project.toml",
    }

    app_policy = parser.parse_args(
        ["app-policy", "--config", "cfg.yaml", "--client", "codex", "--apply", "--label", "L"]
    )
    assert vars(app_policy) == {
        "command": "app-policy",
        "config": "cfg.yaml",
        "client": "codex",
        "apply": True,
        "label": "L",
    }

    rollback = parser.parse_args(["rollback", "--config", "cfg.yaml", "--client", "codex"])
    assert vars(rollback) == {
        "command": "rollback",
        "config": "cfg.yaml",
        "client": "codex",
    }

    help_text = parser.format_help()
    assert "{backup,render,app-policy,rollback}" in help_text

def test_config_render_subcommands_require_config_and_client() -> None:
    parser = _config_render_parser()

    for argv in (
        ["backup", "--client", "codex"],
        ["backup", "--config", "cfg.yaml"],
        ["render", "--client", "codex"],
        ["render", "--config", "cfg.yaml"],
        ["app-policy", "--client", "codex"],
        ["app-policy", "--config", "cfg.yaml"],
        ["rollback", "--client", "codex"],
        ["rollback", "--config", "cfg.yaml"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(argv)
        assert exc_info.value.code == 2

def test_config_render_main_requires_command_and_exposes_help_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_render import CONFIG_RENDER_DESCRIPTION, main

    with pytest.raises(SystemExit) as missing_command:
        main([])
    assert missing_command.value.code == 2

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert CONFIG_RENDER_DESCRIPTION in capsys.readouterr().out

def test_config_render_constants_are_public_contract() -> None:
    from mcp_broker.config_render import CONFIG_RENDER_DESCRIPTION, TEXT_ENCODING, TIMESTAMP_FORMAT

    assert CONFIG_RENDER_DESCRIPTION == "Render or roll back MCP client configs"
    assert TEXT_ENCODING == "utf-8"
    assert TIMESTAMP_FORMAT == "%Y%m%dT%H%M%SZ"

def test_timestamp_label_uses_utc_and_release_label_format(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_broker.config_render as config_render

    class RecordingDateTime:
        timezone_seen = None
        format_seen = None

        @classmethod
        def now(cls, timezone_value):
            cls.timezone_seen = timezone_value
            return cls()

        def strftime(self, format_value: str) -> str:
            type(self).format_seen = format_value
            return "20260525T111213Z"

    monkeypatch.setattr(config_render, "datetime", RecordingDateTime)

    assert config_render._timestamp_label() == "20260525T111213Z"
    assert RecordingDateTime.timezone_seen is config_render.timezone.utc
    assert RecordingDateTime.format_seen == config_render.TIMESTAMP_FORMAT

def test_strip_codex_mcp_tables_removes_only_mcp_tables_and_legacy_blocks() -> None:
    from mcp_broker.config_render import _strip_codex_mcp_tables

    assert _strip_codex_mcp_tables(
        'model = "configured"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "# === MCP servers (synced from ~/mcp/servers.json + project .mcp.json files) ===\n"
        "# generated legacy comment\n"
        "[mcp_servers]\n"
        'command = "root-direct"\n'
        "\n"
        '[mcp_servers."direct-tool"]\n'
        'command = "direct-tool"\n'
        "\n"
        "[profiles.prod]\n"
        'approval_policy = "never"\n'
    ) == (
        'model = "configured"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "[profiles.prod]\n"
        'approval_policy = "never"\n'
    )

def test_strip_codex_mcp_tables_preserves_pending_separator_when_not_legacy() -> None:
    from mcp_broker.config_render import _strip_codex_mcp_tables

    assert _strip_codex_mcp_tables(
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'model = "configured"\n'
    ) == (
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "[profiles.dev]\n"
        'model = "configured"\n'
    )

def test_strip_codex_mcp_tables_preserves_leading_whitespace_and_malformed_headers() -> None:
    from mcp_broker.config_render import _strip_codex_mcp_tables

    assert _strip_codex_mcp_tables(
        "\n"
        "  # local setting\n"
        "[mcp_servers\n"
        'command = "not-a-table"\n'
        "not-a-table]\n"
        "\n"
        "[profiles.dev]\n"
        'model = "configured"\n'
    ) == (
        "\n"
        "  # local setting\n"
        "[mcp_servers\n"
        'command = "not-a-table"\n'
        "not-a-table]\n"
        "\n"
        "[profiles.dev]\n"
        'model = "configured"\n'
    )

def test_strip_codex_mcp_tables_keeps_legacy_block_until_valid_table_header() -> None:
    from mcp_broker.config_render import _strip_codex_mcp_tables

    assert _strip_codex_mcp_tables(
        'model = "configured"\n'
        "# === MCP servers (synced from ~/mcp/servers.json + project .mcp.json files) ===\n"
        "[not-a-table\n"
        "not-a-table]\n"
        "comment in legacy block\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
    ) == (
        'model = "configured"\n'
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n'
    )

def test_strip_trailing_separator_comments_removes_repeated_trailing_blocks() -> None:
    from mcp_broker.config_render import _strip_trailing_separator_comments

    assert _strip_trailing_separator_comments(
        'model = "configured"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
    ) == 'model = "configured"\n'

def test_strip_trailing_separator_comments_preserves_leading_blank_lines() -> None:
    from mcp_broker.config_render import _strip_trailing_separator_comments

    assert _strip_trailing_separator_comments(
        "\n"
        'model = "configured"\n'
        "\n"
        "# -----------------------------------------------------------------------------\n"
        "\n"
    ) == "\n" 'model = "configured"\n'

def test_render_claude_json_preserves_settings_and_sorts_keys() -> None:
    from mcp_broker.config_render import _render_claude_json

    rendered = _render_claude_json(
        "mcp-broker",
        "mcp-broker-client",
        ["--profile", "codex"],
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {"direct": {"command": "direct-client"}},
            }
        ),
    )

    assert rendered == json.dumps(
        {
            "mcpServers": {
                "mcp-broker": {
                    "args": ["--profile", "codex"],
                    "command": "mcp-broker-client",
                }
            },
            "theme": "dark",
        },
        indent=2,
        sort_keys=True,
    ) + "\n"

def test_render_mcp_settings_json_preserves_settings_and_allowed_policy() -> None:
    from mcp_broker.client_config import ClientRenderConfig
    from mcp_broker.config_render import _render_mcp_settings_json

    rendered = _render_mcp_settings_json(
        "mcp-broker",
        "mcp-broker-client",
        ["--profile", "agy"],
        json.dumps(
            {
                "theme": "dark",
                "mcp": {"excluded": ["direct"]},
                "mcpServers": {"direct": {"command": "direct-client"}},
            }
        ),
        ClientRenderConfig(
            name="agy",
            format="mcp-settings-json",
            config_path=Path("settings.json"),
            mcp_allowed_servers=("mcp-broker",),
        ),
    )

    assert rendered == json.dumps(
        {
            "mcp": {"allowed": ["mcp-broker"], "excluded": ["direct"]},
            "mcpServers": {
                "mcp-broker": {
                    "args": ["--profile", "agy"],
                    "command": "mcp-broker-client",
                }
            },
            "theme": "dark",
        },
        indent=2,
        sort_keys=True,
    ) + "\n"

def test_render_mcp_settings_json_does_not_create_mcp_policy_without_allowed_servers() -> None:
    from mcp_broker.client_config import ClientRenderConfig
    from mcp_broker.config_render import _render_mcp_settings_json

    rendered = _render_mcp_settings_json(
        "mcp-broker",
        "mcp-broker-client",
        ["--profile", "agy"],
        "{}",
        ClientRenderConfig(
            name="agy",
            format="mcp-settings-json",
            config_path=Path("settings.json"),
        ),
    )

    assert json.loads(rendered) == {
        "mcpServers": {
            "mcp-broker": {
                "args": ["--profile", "agy"],
                "command": "mcp-broker-client",
            }
        }
    }

def test_json_line_serializes_dataclasses_paths_none_and_bool_values() -> None:
    from mcp_broker.config_render import RenderResult, _json_line

    assert _json_line(
        RenderResult(
            client_name="codex",
            target_path=Path("/tmp/target.toml"),
            rendered_path=Path("/tmp/rendered.toml"),
            backup_path=None,
            dry_run=True,
        )
    ) == (
        '{"backup_path": null, "client_name": "codex", "codex_apps_policy_result": null, '
        '"dry_run": true, "rendered_path": "/tmp/rendered.toml", "target_path": "/tmp/target.toml"}\n'
    )

def test_backup_path_creates_empty_backup_for_missing_source(tmp_path: Path) -> None:
    from mcp_broker.config_render import _backup_path

    config = _config(tmp_path)

    backup_path = _backup_path(
        config,
        "codex",
        tmp_path / "missing.toml",
        backup_label="20260525T101010Z",
    )

    assert backup_path == tmp_path / "runtime" / "backups" / "codex" / "20260525T101010Z.missing.toml"
    assert backup_path.parent.is_dir()
    assert backup_path.read_text(encoding="utf-8") == ""

def test_backup_path_generates_utc_label_when_label_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.config_render as config_render

    monkeypatch.setattr(config_render, "_timestamp_label", lambda: "20260525T111213Z")
    config = _config(tmp_path)

    backup_path = config_render._backup_path(
        config,
        "codex",
        tmp_path / "missing.toml",
        backup_label=None,
    )

    assert backup_path == tmp_path / "runtime" / "backups" / "codex" / "20260525T111213Z.missing.toml"
    assert backup_path.read_text(encoding="utf-8") == ""

def test_rollback_restores_latest_lexicographic_backup(tmp_path: Path) -> None:
    from mcp_broker.config_render import rollback_client_config

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    backup_dir = tmp_path / "runtime" / "backups" / "codex"
    backup_dir.mkdir(parents=True)
    older_backup = backup_dir / "20250101T000000Z.codex.toml"
    newer_backup = backup_dir / "20260101T000000Z.codex.toml"
    older_backup.write_text("older = true\n", encoding="utf-8")
    newer_backup.write_text("newer = true\n", encoding="utf-8")
    target_path.write_text("current = true\n", encoding="utf-8")

    result = rollback_client_config(config, client_name="codex")

    assert result.client_name == "codex"
    assert result.target_path == target_path
    assert result.restored_path == newer_backup
    assert target_path.read_text(encoding="utf-8") == "newer = true\n"

def test_dry_run_render_writes_rendered_artifact_without_target_backup_or_policy(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text('model = "configured"\n', encoding="utf-8")

    result = render_client_config(config, client_name="codex", dry_run=True)

    assert result.client_name == "codex"
    assert result.target_path == target_path
    assert result.rendered_path == tmp_path / "runtime" / "renders" / "codex.config.toml"
    assert result.backup_path is None
    assert result.dry_run is True
    assert result.codex_apps_policy_result is None
    assert target_path.read_text(encoding="utf-8") == 'model = "configured"\n'
    assert '[mcp_servers."mcp-broker"]' in result.rendered_path.read_text(encoding="utf-8")

def test_render_text_missing_source_starts_with_broker_entry_only(tmp_path: Path) -> None:
    from mcp_broker.config_render import _render_text

    config = _config(tmp_path)
    client = config.clients["codex"]

    assert _render_text(config, client) == (
        '[mcp_servers."mcp-broker"]\n'
        'command = "mcp-broker-client"\n'
        f'args = ["--socket-path", "{tmp_path}/runtime/sockets/broker.sock"]\n'
    )
