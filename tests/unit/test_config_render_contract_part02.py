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

def test_codex_render_text_escapes_toml_and_strips_only_mcp_server_tables(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "codex.toml"
    target_path.write_text(
        'model = "configured-model"\n'
        "\n"
        "[mcp_servers]\n"
        'command = "remove-root-table"\n'
        "\n"
        '[mcp_servers."direct-reader"]\n'
        'command = "remove-nested-table"\n'
        "\n"
        "[profiles.dev]\n"
        'sandbox_mode = "danger-full-access"\n',
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
                    "toml-client": {
                        "format": "codex-toml",
                        "config_path": str(target_path),
                        "entry_name": 'broker "quoted"',
                        "command": 'client "runner"',
                        "args": [
                            "--socket-path",
                            "{runtime.socket_path}",
                            "--profile",
                            'profile "quoted"',
                            r"C:\client\path",
                        ],
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
        dry_run=True,
    )

    rendered = result.rendered_path.read_text(encoding="utf-8")
    assert 'model = "configured-model"' in rendered
    assert "[profiles.dev]" in rendered
    assert "direct-reader" not in rendered
    assert "remove-root-table" not in rendered
    assert "remove-nested-table" not in rendered
    assert '[mcp_servers."broker \\"quoted\\""]' in rendered
    assert 'command = "client \\"runner\\""' in rendered
    assert (
        f'args = ["--socket-path", "{runtime_root}/sockets/broker.sock", "--profile", '
        '"profile \\"quoted\\"", "C:\\\\client\\\\path"]'
    ) in rendered

def test_codex_mcp_table_classifier_handles_root_nested_and_non_mcp_headers() -> None:
    from mcp_broker.config_render import _is_codex_mcp_table, _is_table_header

    assert _is_table_header("[mcp_servers]") is True
    assert _is_table_header("[mcp_servers") is False
    assert _is_table_header("mcp_servers]") is False
    assert _is_codex_mcp_table("[mcp_servers]") is True
    assert _is_codex_mcp_table('[mcp_servers."generic-tool"]') is True
    assert _is_codex_mcp_table("[profiles.dev]") is False
    assert _is_codex_mcp_table("[mcp_serverish]") is False

def test_mcp_settings_json_render_preserves_client_settings_and_replaces_servers(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "settings.json"
    target_path.write_text(
        json.dumps(
            {
                "selectedAuthType": "api-key",
                "mcpServers": {
                    "direct-memory": {
                        "command": "memory-mcp",
                    },
                },
            },
            sort_keys=True,
        ),
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
                    "json-chat-client": {
                        "format": "mcp-settings-json",
                        "config_path": str(target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "args": [
                            "--socket-path",
                            "{runtime.socket_path}",
                            "--profile",
                            "json-chat-client",
                        ],
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
        client_name="json-chat-client",
        dry_run=True,
    )

    assert json.loads(target_path.read_text(encoding="utf-8"))["mcpServers"] == {
        "direct-memory": {
            "command": "memory-mcp",
        }
    }
    rendered = json.loads(result.rendered_path.read_text(encoding="utf-8"))
    assert rendered == {
        "mcpServers": {
            "mcp-broker": {
                "args": [
                    "--socket-path",
                    str(runtime_root / "sockets" / "broker.sock"),
                    "--profile",
                    "json-chat-client",
                ],
                "command": "mcp-broker-client",
            }
        },
        "selectedAuthType": "api-key",
    }

def test_agy_settings_json_apply_writes_allowed_broker_and_backup(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "settings.json"
    target_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcp": {"excluded": ["legacy-direct"]},
                "mcpServers": {"legacy-direct": {"command": "legacy-client"}},
            },
            sort_keys=True,
        ),
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
                    "agy": {
                        "format": "mcp-settings-json",
                        "config_path": str(target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "args": [
                            "--socket-path",
                            "{runtime.socket_path}",
                            "--profile",
                            "agy",
                        ],
                        "mcp_allowed_servers": ["mcp-broker"],
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
        client_name="agy",
        dry_run=False,
        backup_label="20260525T010101Z",
    )

    assert result.backup_path == runtime_root / "backups" / "agy" / "20260525T010101Z.settings.json"
    assert json.loads(result.backup_path.read_text(encoding="utf-8"))["mcpServers"] == {
        "legacy-direct": {"command": "legacy-client"}
    }
    rendered = json.loads(target_path.read_text(encoding="utf-8"))
    assert rendered == {
        "mcp": {"allowed": ["mcp-broker"], "excluded": ["legacy-direct"]},
        "mcpServers": {
            "mcp-broker": {
                "args": [
                    "--socket-path",
                    str(runtime_root / "sockets" / "broker.sock"),
                    "--profile",
                    "agy",
                ],
                "command": "mcp-broker-client",
            }
        },
        "theme": "dark",
    }

def test_render_client_config_uses_explicit_text_encoding_for_reads_and_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.config_render as config_render

    config = _config(tmp_path)
    target_path = tmp_path / "codex.toml"
    target_path.write_text('model = "configured"\n', encoding="utf-8")
    missing_source = tmp_path / "missing.toml"
    rendered_path = tmp_path / "runtime" / "renders" / "codex.config.toml"
    missing_backup_path = tmp_path / "runtime" / "backups" / "codex" / "20260525T121212Z.missing.toml"
    real_read_text = Path.read_text
    real_write_text = Path.write_text
    read_paths: list[Path] = []
    write_paths: list[Path] = []

    def checked_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == target_path:
            assert kwargs.get("encoding") == config_render.TEXT_ENCODING
            read_paths.append(self)
        return real_read_text(self, *args, **kwargs)

    def checked_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self in {rendered_path, target_path, missing_backup_path}:
            assert kwargs.get("encoding") == config_render.TEXT_ENCODING
            write_paths.append(self)
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read_text)
    monkeypatch.setattr(Path, "write_text", checked_write_text)

    config_render.render_client_config(
        config,
        client_name="codex",
        dry_run=False,
        backup_label="20260525T121212Z",
    )
    config_render._backup_path(
        config,
        "codex",
        missing_source,
        backup_label="20260525T121212Z",
    )

    assert read_paths == [target_path]
    assert rendered_path in write_paths
    assert target_path in write_paths
    assert missing_backup_path in write_paths

def test_claude_render_preserves_user_settings_replaces_servers_and_records_backup(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "claude.json"
    target_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {"legacy-direct": {"command": "legacy-client"}},
            },
            sort_keys=True,
        ),
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
                    "claude": {
                        "format": "claude-json",
                        "config_path": str(target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "args": [
                            "--socket-path",
                            "{runtime.socket_path}",
                            "--profile",
                            "claude",
                        ],
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
        client_name="claude",
        dry_run=False,
        backup_label="20260525T020202Z",
    )

    assert result.backup_path == runtime_root / "backups" / "claude" / "20260525T020202Z.claude.json"
    assert json.loads(result.backup_path.read_text(encoding="utf-8"))["mcpServers"] == {
        "legacy-direct": {"command": "legacy-client"}
    }
    assert json.loads(target_path.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "mcp-broker": {
                "args": [
                    "--socket-path",
                    str(runtime_root / "sockets" / "broker.sock"),
                    "--profile",
                    "claude",
                ],
                "command": "mcp-broker-client",
            }
        },
        "theme": "dark",
    }

def test_apply_render_creates_nested_override_target_parent(tmp_path: Path) -> None:
    from mcp_broker.config_render import render_client_config

    config = _config(tmp_path)
    override_path = tmp_path / "clients" / "nested" / "codex.toml"

    result = render_client_config(
        config,
        client_name="codex",
        dry_run=False,
        target_path=override_path,
        backup_label="20260525T131313Z",
    )

    assert result.target_path == override_path
    assert override_path.read_text(encoding="utf-8").startswith('[mcp_servers."mcp-broker"]\n')
    assert result.backup_path == (
        tmp_path / "runtime" / "backups" / "codex" / "20260525T131313Z.codex.toml"
    )

def test_mcp_settings_json_render_can_write_allowed_mcp_server_policy(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "settings.json"
    target_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "excluded": ["legacy-server"],
                },
                "mcpServers": {
                    "legacy-server": {
                        "command": "legacy-mcp",
                    },
                },
            },
            sort_keys=True,
        ),
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
                    "local-chat": {
                        "format": "mcp-settings-json",
                        "config_path": str(target_path),
                        "entry_name": "mcp-broker",
                        "command": "mcp-broker-client",
                        "args": [
                            "--socket-path",
                            "{runtime.socket_path}",
                            "--profile",
                            "local-chat",
                        ],
                        "mcp_allowed_servers": ["mcp-broker"],
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
        client_name="local-chat",
        dry_run=True,
    )

    rendered = json.loads(result.rendered_path.read_text(encoding="utf-8"))
    assert rendered["mcp"] == {
        "allowed": ["mcp-broker"],
        "excluded": ["legacy-server"],
    }
    assert rendered["mcpServers"] == {
        "mcp-broker": {
            "args": [
                "--socket-path",
                str(runtime_root / "sockets" / "broker.sock"),
                "--profile",
                "local-chat",
            ],
            "command": "mcp-broker-client",
        }
    }
