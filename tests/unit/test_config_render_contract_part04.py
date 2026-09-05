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

def test_config_render_apply_enforces_codex_apps_policy(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    app_directory = tmp_path / "codex-cache" / "app-directory" / "directory.json"
    tools_cache = tmp_path / "codex-cache" / "tools" / "tools.json"
    app_directory.parent.mkdir(parents=True)
    tools_cache.parent.mkdir(parents=True)
    app_directory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connectors": [
                    {"id": "connector_github", "name": "GitHub", "isEnabled": True},
                    {"id": "connector_canva", "name": "Canva", "isEnabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    tools_cache.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tools": [
                    {"connector_id": "connector_github", "connector_name": "GitHub", "tool": {}},
                    {"connector_id": "connector_canva", "connector_name": "Canva", "tool": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = _config_file(
        tmp_path,
        client_extra={
            "codex_apps_policy": {
                "enabled": True,
                "app_directory_globs": [str(app_directory)],
                "tools_cache_globs": [str(tools_cache)],
                "disable_connectors": [
                    {
                        "id": "connector_github",
                        "name": "GitHub",
                        "reason": "broker owns it",
                    }
                ],
            }
        },
    )

    result = render_client_config(
        BrokerConfig.from_file(config_path),
        client_name="codex",
        dry_run=False,
        backup_label="20260524T040404Z",
    )

    assert result.codex_apps_policy_result is not None
    assert result.codex_apps_policy_result.disabled_connectors == 1
    assert result.codex_apps_policy_result.removed_tools == 1
    assert result.codex_apps_policy_result.backups == (
        tmp_path / "runtime" / "backups" / "codex" / "codex-apps" / "20260524T040404Z.directory.json",
        tmp_path / "runtime" / "backups" / "codex" / "codex-apps" / "20260524T040404Z.tools.json",
    )
    assert json.loads(app_directory.read_text(encoding="utf-8"))["connectors"] == [
        {"id": "connector_github", "name": "GitHub", "isEnabled": False},
        {"id": "connector_canva", "name": "Canva", "isEnabled": True},
    ]
    assert [tool["connector_name"] for tool in json.loads(tools_cache.read_text(encoding="utf-8"))["tools"]] == [
        "Canva"
    ]

def test_config_render_dry_run_reports_codex_apps_policy_without_changing_cache(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    app_directory = tmp_path / "app-cache" / "directory.json"
    app_directory.parent.mkdir(parents=True)
    original_cache = {
        "schema_version": 1,
        "connectors": [
            {"id": "connector_docs", "name": "Docs", "isEnabled": True},
            {"id": "connector_design", "name": "Design", "isEnabled": True},
        ],
    }
    app_directory.write_text(json.dumps(original_cache), encoding="utf-8")
    config_path = _config_file(
        tmp_path,
        client_extra={
            "codex_apps_policy": {
                "enabled": True,
                "app_directory_globs": [str(app_directory)],
                "tools_cache_globs": [],
                "disable_connectors": [
                    {
                        "id": "connector_docs",
                        "name": "Docs",
                        "reason": "broker owns this connector",
                    }
                ],
            }
        },
    )

    result = render_client_config(
        BrokerConfig.from_file(config_path),
        client_name="codex",
        dry_run=True,
        backup_label="20260525T030303Z",
    )

    assert result.backup_path is None
    assert result.codex_apps_policy_result is not None
    assert result.codex_apps_policy_result.dry_run is True
    assert result.codex_apps_policy_result.disabled_connectors == 1
    assert json.loads(app_directory.read_text(encoding="utf-8")) == original_cache
    assert not (tmp_path / "runtime" / "backups" / "codex" / "codex-apps").exists()

def test_config_render_app_policy_cli_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.config_render import main

    app_directory = tmp_path / "codex-cache" / "app-directory" / "directory.json"
    app_directory.parent.mkdir(parents=True)
    app_directory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connectors": [
                    {"id": "connector_github", "name": "GitHub", "isEnabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = _config_file(
        tmp_path,
        client_extra={
            "codex_apps_policy": {
                "enabled": True,
                "app_directory_globs": [str(app_directory)],
                "tools_cache_globs": [],
                "disable_connectors": [{"id": "connector_github"}],
            }
        },
    )

    assert (
        main(
            [
                "app-policy",
                "--config",
                str(config_path),
                "--client",
                "codex",
                "--label",
                "20260524T050505Z",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["client_name"] == "codex"
    assert output["codex_apps_policy_result"]["disabled_connectors"] == 1
    assert output["codex_apps_policy_result"]["dry_run"] is True
    assert json.loads(app_directory.read_text(encoding="utf-8"))["connectors"][0]["isEnabled"] is True

def test_config_render_app_policy_cli_apply_writes_cache_and_labeled_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_render import main

    app_directory = tmp_path / "app-cache" / "directory.json"
    app_directory.parent.mkdir(parents=True)
    app_directory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connectors": [
                    {"id": "connector_docs", "name": "Docs", "isEnabled": True},
                    {"id": "connector_design", "name": "Design", "isEnabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = _config_file(
        tmp_path,
        client_extra={
            "codex_apps_policy": {
                "enabled": True,
                "app_directory_globs": [str(app_directory)],
                "tools_cache_globs": [],
                "disable_connectors": [{"id": "connector_docs"}],
            }
        },
    )

    assert (
        main(
            [
                "app-policy",
                "--config",
                str(config_path),
                "--client",
                "codex",
                "--apply",
                "--label",
                "20260525T040404Z",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    result = output["codex_apps_policy_result"]
    assert result["dry_run"] is False
    assert result["disabled_connectors"] == 1
    assert result["backups"] == [
        str(tmp_path / "runtime" / "backups" / "codex" / "codex-apps" / "20260525T040404Z.directory.json")
    ]
    assert json.loads(app_directory.read_text(encoding="utf-8"))["connectors"] == [
        {"id": "connector_docs", "name": "Docs", "isEnabled": False},
        {"id": "connector_design", "name": "Design", "isEnabled": True},
    ]

def test_config_render_app_policy_generates_timestamp_label_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.config_render as config_render

    from mcp_broker.config import BrokerConfig

    app_directory = tmp_path / "app-cache" / "directory.json"
    app_directory.parent.mkdir(parents=True)
    app_directory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connectors": [
                    {"id": "connector_docs", "name": "Docs", "isEnabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = _config_file(
        tmp_path,
        client_extra={
            "codex_apps_policy": {
                "enabled": True,
                "app_directory_globs": [str(app_directory)],
                "tools_cache_globs": [],
                "disable_connectors": [{"id": "connector_docs"}],
            }
        },
    )
    monkeypatch.setattr(config_render, "_timestamp_label", lambda: "20260525T111213Z")

    result = config_render.apply_client_app_policy(
        BrokerConfig.from_file(config_path),
        client_name="codex",
        dry_run=False,
    )

    assert result.codex_apps_policy_result.backups == (
        tmp_path / "runtime" / "backups" / "codex" / "codex-apps" / "20260525T111213Z.directory.json",
    )
    assert json.loads(app_directory.read_text(encoding="utf-8"))["connectors"] == [
        {"id": "connector_docs", "name": "Docs", "isEnabled": False},
    ]

def test_claude_render_replaces_non_object_json_with_broker_config(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.config import BrokerConfig
    from mcp_broker.config_render import render_client_config

    runtime_root = tmp_path / "runtime"
    target_path = tmp_path / "claude.json"
    target_path.write_text(json.dumps(["not", "a", "config"]), encoding="utf-8")
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
                        "args": ["--socket-path", str(runtime_root / "sockets" / "broker.sock"), "--profile", "claude"],
                    }
                },
                "upstreams": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    render_client_config(BrokerConfig.from_file(config_path), client_name="claude", dry_run=True)

    rendered = json.loads((runtime_root / "renders" / "claude.config.json").read_text(encoding="utf-8"))
    assert rendered == {
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
        }
    }

def test_json_line_rejects_unknown_object_type() -> None:
    from mcp_broker.config_render import _json_line

    with pytest.raises(TypeError, match="cannot encode object"):
        _json_line(object())
