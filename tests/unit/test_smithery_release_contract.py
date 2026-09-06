import json
import urllib.request
import zipfile
from pathlib import Path

import pytest

from scripts.smithery_release import _http_request, build_payload_from_manifest, load_mcpb_manifest


pytestmark = pytest.mark.unit

FIXTURE_VERSION = ".".join(("1", "1", "0"))
RICH_SCHEMA_FIXTURE_VERSION = ".".join(("1", "1", "1"))


def test_smithery_payload_adds_tool_input_schemas_without_changing_mcpb_manifest(
    tmp_path: Path,
) -> None:
    manifest = {
        "name": "mcp-broker",
        "version": FIXTURE_VERSION,
        "server": {"type": "binary", "mcp_config": {"command": "uvx"}},
        "tools": [
            {"name": "custom_status", "description": "Report custom health."},
            {
                "name": "custom_call_tool",
                "description": "Call one upstream tool.",
                "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        ],
        "user_config": {},
    }

    bundle_path = tmp_path / "server.mcpb"
    with zipfile.ZipFile(bundle_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    loaded = load_mcpb_manifest(bundle_path)
    payload = build_payload_from_manifest(loaded)

    assert loaded["tools"][0] == {"name": "custom_status", "description": "Report custom health."}
    assert payload["type"] == "stdio"
    assert payload["runtime"] == "binary"
    assert payload["serverCard"]["tools"] == [
        {
            "name": "custom_status",
            "description": "Report custom health.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "custom_call_tool",
            "description": "Call one upstream tool.",
            "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    ]


def test_smithery_payload_adds_rich_broker_tool_schemas_from_source_contract() -> None:
    from mcp_broker.tool_namespace import compact_broker_tool_definitions

    manifest = {
        "name": "mcp-broker",
        "version": RICH_SCHEMA_FIXTURE_VERSION,
        "server": {"type": "binary", "mcp_config": {"command": "uvx"}},
        "tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
            }
            for tool in compact_broker_tool_definitions(broker_tool_name_style="snake")
        ],
        "user_config": {},
    }

    payload = build_payload_from_manifest(manifest)
    tools = {tool["name"]: tool for tool in payload["serverCard"]["tools"]}

    assert tools["broker_search_tools"]["inputSchema"]["properties"]["query"]["description"]
    assert tools["broker_search_tools"]["inputSchema"]["properties"]["limit"]["default"] == 20
    assert (
        tools["broker_call_tool"]["inputSchema"]["properties"]["arguments"]["additionalProperties"]
        is True
    )
    assert tools["broker_status"]["inputSchema"]["additionalProperties"] is False


def test_smithery_payload_converts_mcpb_user_config_to_json_schema() -> None:
    manifest = {
        "name": "mcp-broker",
        "version": FIXTURE_VERSION,
        "server": {"type": "binary", "mcp_config": {"command": "${user_config.uvx_path}"}},
        "tools": [],
        "user_config": {
            "uvx_path": {
                "type": "string",
                "title": "UVX command path",
                "description": "Command or absolute path for uvx.",
                "required": True,
                "default": "uvx",
            },
            "runtime_root": {
                "type": "directory",
                "title": "Runtime directory",
                "required": True,
                "default": "${HOME}/mcp/mcp-broker",
            },
            "config_path": {
                "type": "file",
                "title": "Config path",
                "required": False,
                "default": "${HOME}/mcp/mcp-broker/config/broker.yaml",
            },
        },
    }

    payload = build_payload_from_manifest(manifest)

    assert payload["configSchema"] == {
        "type": "object",
        "properties": {
            "uvx_path": {
                "type": "string",
                "title": "UVX command path",
                "description": "Command or absolute path for uvx.",
                "default": "uvx",
            },
            "runtime_root": {
                "type": "string",
                "title": "Runtime directory",
                "default": "${HOME}/mcp/mcp-broker",
            },
            "config_path": {
                "type": "string",
                "title": "Config path",
                "default": "${HOME}/mcp/mcp-broker/config/broker.yaml",
            },
        },
        "required": ["uvx_path", "runtime_root"],
    }


def test_smithery_release_adapter_does_not_depend_on_transitive_requests() -> None:
    script = Path("scripts/smithery_release.py").read_text(encoding="utf-8")

    assert "import requests" not in script
    assert "urllib.request" in script


@pytest.mark.error_simulation
def test_smithery_http_request_uses_configured_user_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("SMITHERY_USER_AGENT", "mcp-broker-test-client")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def urlopen(request: urllib.request.Request, *, timeout: int):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    response = _http_request(
        method="PUT",
        url="https://api.example.invalid/releases",
        api_key="test-api-key",
        body=b"{}",
        content_type="application/json",
        timeout=37,
    )

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.get_header("User-agent") == "mcp-broker-test-client"
    assert captured["timeout"] == 37
    assert response.status_code == 200


def test_smithery_http_request_requires_configured_user_agent(monkeypatch) -> None:
    monkeypatch.delenv("SMITHERY_USER_AGENT", raising=False)

    with pytest.raises(
        RuntimeError,
        match="SMITHERY_USER_AGENT is required for Smithery API requests",
    ):
        _http_request(
            method="PUT",
            url="https://api.example.invalid/releases",
            api_key="test-api-key",
            body=b"{}",
            content_type="application/json",
            timeout=37,
        )
