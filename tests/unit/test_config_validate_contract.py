from pathlib import Path
from io import StringIO

import pytest

from tests.support.repo_paths import repo_root


pytestmark = pytest.mark.unit

ROOT = repo_root()
PUBLIC_CONFIG_FILE = ROOT / "config" / "broker.example.yaml"
SCHEMA_FILE = ROOT / "config" / "broker.schema.json"


def test_public_json_schema_exists_and_names_contract() -> None:
    import json

    loaded = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert loaded["$id"] == "https://mcp-broker.local/schema/broker.schema.json"
    assert loaded["title"] == "mcp-broker config"
    assert loaded["properties"]["schema_version"]["const"] == 1
    assert loaded["additionalProperties"] is False


def test_config_validate_accepts_public_example() -> None:
    from mcp_broker.config_validate import validate_config_file

    result = validate_config_file(PUBLIC_CONFIG_FILE, SCHEMA_FILE)

    assert result.config_path == PUBLIC_CONFIG_FILE
    assert result.schema_path == SCHEMA_FILE
    assert result.ok is True


def test_config_validate_accepts_transport_header_request_metadata(tmp_path: Path) -> None:
    from mcp_broker.config_validate import validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  node-repl:
    command: node
    env:
      CODEX_TURN_METADATA: present
    request_meta:
      x-codex-turn-metadata: CODEX_TURN_METADATA
""".strip(),
        encoding="utf-8",
    )

    result = validate_config_file(config_file, SCHEMA_FILE)

    assert result.ok is True


def test_config_validate_rejects_unknown_keys_before_runtime_load(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: read-store
    unexpected_private_fixture: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as raised:
        validate_config_file(config_file, SCHEMA_FILE)

    assert "schema validation failed" in str(raised.value)
    assert "upstreams.read-store" in str(raised.value)
    assert "unexpected_private_fixture" in str(raised.value)


def test_config_validate_rejects_session_env_without_per_session_mode(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  session-tool:
    command: session-tool
    mode: shared
    session_env:
      PROJECT_DIR: client_cwd
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as raised:
        validate_config_file(config_file, SCHEMA_FILE)

    assert "schema validation failed" in str(raised.value)
    assert "upstreams.session-tool.mode" in str(raised.value)
    assert "per_session" in str(raised.value)
    assert "per_call" in str(raised.value)


def test_config_validate_accepts_session_env_with_per_call_mode(tmp_path: Path) -> None:
    from mcp_broker.config_validate import validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  task-runner:
    command: task-runner
    mode: per_call
    session_env:
      PROJECT_DIR: client_cwd
""".strip(),
        encoding="utf-8",
    )

    validate_config_file(config_file, SCHEMA_FILE)


def test_config_validate_rejects_per_call_http_transport(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  remote-task:
    command: https://example.invalid/mcp
    mode: per_call
    transport: http
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as raised:
        validate_config_file(config_file, SCHEMA_FILE)

    assert "schema validation failed" in str(raised.value)
    assert "upstreams.remote-task.transport" in str(raised.value)
    assert "stdio" in str(raised.value)


def test_config_validate_reports_runtime_loader_errors(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
broker: {}
profiles:
  codex:
    max_tools: 80
    compact_tools_enabled: true
upstreams:
  writer:
    command: writer
    profiles:
      - codex
    mutating: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigValidationError) as raised:
        validate_config_file(config_file, SCHEMA_FILE)

    assert "loader validation failed" in str(raised.value)
    assert "mutating upstream writer requires profile allowlist entry: codex" in str(raised.value)


def test_config_validate_rejects_non_object_schema_and_config(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    schema_file = tmp_path / "schema.json"
    schema_file.write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="schema file must contain a JSON object"):
        validate_config_file(PUBLIC_CONFIG_FILE, schema_file)

    config_file = tmp_path / "broker.yaml"
    config_file.write_text("[]", encoding="utf-8")

    with pytest.raises(ConfigValidationError, match="config file must contain a YAML mapping"):
        validate_config_file(config_file, SCHEMA_FILE)


def test_config_validate_wraps_malformed_json_and_yaml(tmp_path: Path) -> None:
    from mcp_broker.config_validate import ConfigValidationError, validate_config_file

    schema_file = tmp_path / "schema.json"
    schema_file.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigValidationError) as schema_error:
        validate_config_file(PUBLIC_CONFIG_FILE, schema_file)

    assert str(schema_error.value).startswith("schema file must contain valid JSON:")
    assert str(schema_file) in str(schema_error.value)

    config_file = tmp_path / "broker.yaml"
    config_file.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(ConfigValidationError) as config_error:
        validate_config_file(config_file, SCHEMA_FILE)

    assert str(config_error.value).startswith("config file must contain valid YAML:")
    assert str(config_file) in str(config_error.value)


def test_config_validate_formats_root_schema_errors_and_sorts_paths() -> None:
    from mcp_broker.config_validate import ConfigValidationError, _validate_json_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["alpha"],
        "properties": {"alpha": {"type": "string"}},
    }

    with pytest.raises(ConfigValidationError) as raised:
        _validate_json_schema({"beta": 1}, schema)

    assert str(raised.value) == (
        "schema validation failed: <root>: 'alpha' is a required property; "
        "<root>: Additional properties are not allowed ('beta' was unexpected)"
    )

    nested_schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
        },
    }

    with pytest.raises(ConfigValidationError) as nested_error:
        _validate_json_schema({"b": "text", "a": 1}, nested_schema)

    assert str(nested_error.value) == (
        "schema validation failed: a: 1 is not of type 'string'; "
        "b: 'text' is not of type 'integer'"
    )


def test_config_validate_loaders_read_utf8_text() -> None:
    from mcp_broker.config_validate import _load_json_mapping, _load_yaml_mapping

    class FakePath:
        def __init__(self, text: str) -> None:
            self.text = text
            self.calls: list[tuple[str, str | None]] = []

        def open(self, mode: str, *, encoding: str | None = None) -> StringIO:
            self.calls.append((mode, encoding))
            return StringIO(self.text)

        def __str__(self) -> str:
            return "fake-config-path"

    json_path = FakePath('{"name": "ok"}')
    yaml_path = FakePath("name: ok\n")

    assert _load_json_mapping(json_path) == {"name": "ok"}  # type: ignore[arg-type]
    assert _load_yaml_mapping(yaml_path) == {"name": "ok"}  # type: ignore[arg-type]
    assert json_path.calls == [("r", "utf-8")]
    assert yaml_path.calls == [("r", "utf-8")]


def test_config_validate_parser_contract(capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.config_validate import _parse_args

    args = _parse_args(["--config", "broker.yaml", "--schema", "broker.schema.json"])

    assert args.config == Path("broker.yaml")
    assert args.schema == Path("broker.schema.json")

    with pytest.raises(SystemExit) as missing_config:
        _parse_args(["--schema", "broker.schema.json"])
    assert missing_config.value.code == 2

    with pytest.raises(SystemExit) as missing_schema:
        _parse_args(["--config", "broker.yaml"])
    assert missing_schema.value.code == 2

    with pytest.raises(SystemExit) as help_exit:
        _parse_args(["--help"])

    assert help_exit.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[2] == "Validate an mcp-broker YAML config"
    assert "--config" in captured.out
    assert "--schema" in captured.out


def test_config_validate_main_reports_success_and_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_validate import main

    assert main(["--config", str(PUBLIC_CONFIG_FILE), "--schema", str(SCHEMA_FILE)]) == 0
    captured = capsys.readouterr()
    assert "config validated:" in captured.out
    assert str(PUBLIC_CONFIG_FILE) in captured.out

    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("[]", encoding="utf-8")

    assert main(["--config", str(bad_config), "--schema", str(SCHEMA_FILE)]) == 1
    captured = capsys.readouterr()
    assert "config file must contain a YAML mapping" in captured.out
