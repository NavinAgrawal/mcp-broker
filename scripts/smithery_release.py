from __future__ import annotations

import argparse
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any
import urllib.error
from urllib.parse import quote
import urllib.request
import uuid

from mcp_broker.tool_namespace import compact_broker_tool_definitions


LOGGER = logging.getLogger(__name__)
SMITHERY_API_BASE_URL_ENV = "SMITHERY_API_BASE_URL"
DEFAULT_SETTINGS_PATH = Path.home() / "Library/Application Support/smithery/settings.json"
DEFAULT_TOOL_INPUT_SCHEMA = {"type": "object", "properties": {}}
COMPACT_BROKER_TOOL_SCHEMAS = {
    tool["name"]: tool["inputSchema"]
    for tool in compact_broker_tool_definitions(broker_tool_name_style="snake")
}
SUPPORTED_RUNTIMES = {"binary", "python", "node", "bun"}


class ApiResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return json.loads(self.text)


def load_mcpb_manifest(bundle_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(bundle_path) as archive:
        try:
            raw = archive.read("manifest.json")
        except KeyError as exc:
            raise ValueError(f"MCPB bundle missing manifest.json: {bundle_path}") from exc
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("MCPB manifest must be a JSON object")
    return manifest


def build_payload_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime_from_manifest(manifest)
    payload: dict[str, Any] = {
        "type": "stdio",
        "runtime": runtime,
        "serverCard": _server_card_from_manifest(manifest),
    }
    config_schema = _config_schema_from_user_config(manifest.get("user_config"))
    if config_schema is not None:
        payload["configSchema"] = config_schema
    return payload


def load_api_key(settings_path: Path = DEFAULT_SETTINGS_PATH) -> str:
    configured = os.environ.get("SMITHERY_API_KEY", "").strip()
    if configured:
        return configured
    if not settings_path.is_file():
        raise RuntimeError(
            "SMITHERY_API_KEY is not set and Smithery settings.json was not found"
        )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    api_key = str(settings.get("apiKey", "")).strip()
    if not api_key:
        raise RuntimeError("Smithery settings.json does not contain apiKey")
    return api_key


def publish_bundle(
    *,
    bundle_path: Path,
    qualified_name: str,
    base_url: str,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
) -> dict[str, Any]:
    manifest = load_mcpb_manifest(bundle_path)
    payload = build_payload_from_manifest(manifest)
    api_key = load_api_key(settings_path)
    response = _put_release(
        base_url=base_url,
        api_key=api_key,
        qualified_name=qualified_name,
        payload=payload,
        bundle_path=bundle_path,
    )
    if response.status_code == 404:
        _create_server(
            base_url=base_url,
            api_key=api_key,
            qualified_name=qualified_name,
            manifest=manifest,
        )
        response = _put_release(
            base_url=base_url,
            api_key=api_key,
            qualified_name=qualified_name,
            payload=payload,
            bundle_path=bundle_path,
        )
    _raise_for_status(response)
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Smithery publish response was not a JSON object")
    return data


def _runtime_from_manifest(manifest: dict[str, Any]) -> str:
    server = manifest.get("server")
    if not isinstance(server, dict):
        raise ValueError("MCPB manifest missing server object")
    runtime = server.get("type")
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"unsupported Smithery runtime: {runtime}")
    return str(runtime)


def _server_card_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    name = manifest.get("name")
    version = manifest.get("version")
    if not isinstance(name, str) or not name:
        raise ValueError("MCPB manifest missing name")
    if not isinstance(version, str) or not version:
        raise ValueError("MCPB manifest missing version")
    server_info: dict[str, Any] = {"name": name, "version": version}
    if isinstance(manifest.get("description"), str):
        server_info["description"] = manifest["description"]
    tools = [
        _tool_for_server_card(tool)
        for tool in manifest.get("tools", [])
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    card: dict[str, Any] = {"serverInfo": server_info}
    if tools:
        card["tools"] = tools
    return card


def _tool_for_server_card(tool: dict[str, Any]) -> dict[str, Any]:
    converted = dict(tool)
    if "inputSchema" not in converted:
        converted["inputSchema"] = deepcopy_schema(
            COMPACT_BROKER_TOOL_SCHEMAS.get(
                str(converted.get("name")),
                DEFAULT_TOOL_INPUT_SCHEMA,
            )
        )
    return converted


def deepcopy_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(schema))


def _config_schema_from_user_config(user_config: Any) -> dict[str, Any] | None:
    if not isinstance(user_config, dict) or not user_config:
        return None
    schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for key, field in user_config.items():
        if not isinstance(key, str) or not isinstance(field, dict):
            continue
        property_schema = _user_config_field_schema(field)
        schema["properties"][key] = property_schema
        if field.get("required") is True:
            schema["required"].append(key)
    if not schema["required"]:
        schema.pop("required")
    return schema


def _user_config_field_schema(field: dict[str, Any]) -> dict[str, Any]:
    field_type = field.get("type")
    json_type = "string" if field_type in {"directory", "file"} else field_type
    if not isinstance(json_type, str):
        raise ValueError("user_config field missing string type")
    schema: dict[str, Any] = {"type": json_type}
    for key in ("title", "description", "default"):
        if key in field:
            schema[key] = field[key]
    return schema


def _put_release(
    *,
    base_url: str,
    api_key: str,
    qualified_name: str,
    payload: dict[str, Any],
    bundle_path: Path,
) -> Any:
    url = f"{base_url.rstrip('/')}/servers/{quote(qualified_name, safe='')}/releases"
    fields = {"payload": json.dumps(payload, separators=(",", ":"))}
    files = {"bundle": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")}
    body, content_type = _multipart_form_data(fields=fields, files=files)
    return _http_request(
        method="PUT",
        url=url,
        api_key=api_key,
        body=body,
        content_type=content_type,
        timeout=120,
    )


def _create_server(
    *,
    base_url: str,
    api_key: str,
    qualified_name: str,
    manifest: dict[str, Any],
) -> None:
    url = f"{base_url.rstrip('/')}/servers/{quote(qualified_name, safe='')}"
    body = {
        "displayName": str(manifest.get("display_name") or manifest.get("name") or "mcp-broker"),
        "description": str(manifest.get("description") or ""),
    }
    response = _http_request(
        method="PUT",
        url=url,
        api_key=api_key,
        body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
        timeout=60,
    )
    _raise_for_status(response)


def _http_request(
    *,
    method: str,
    url: str,
    api_key: str,
    body: bytes,
    content_type: str,
    timeout: int,
) -> ApiResponse:
    user_agent = os.environ.get("SMITHERY_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SMITHERY_USER_AGENT is required for Smithery API requests")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            return ApiResponse(status_code=response.status, text=text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return ApiResponse(status_code=exc.code, text=text)


def _multipart_form_data(
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"mcp-broker-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, data, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _raise_for_status(response: Any) -> None:
    if 200 <= response.status_code < 300:
        return
    body = response.text
    raise RuntimeError(f"Smithery API failed with HTTP {response.status_code}: {body}")


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish MCPB bundles to Smithery.")
    parser.add_argument("bundle", type=Path, help="Path to the .mcpb bundle")
    parser.add_argument("-n", "--name", required=True, help="Smithery qualified name")
    parser.add_argument(
        "--base-url",
        default=os.environ.get(SMITHERY_API_BASE_URL_ENV, ""),
        help="Smithery API base URL",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="Smithery CLI settings path",
    )
    parser.add_argument("--dry-run", action="store_true", help="Write payload without publishing")
    parser.add_argument("--payload-output", type=Path, help="Path for dry-run payload JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.base_url:
        raise RuntimeError(f"{SMITHERY_API_BASE_URL_ENV} or --base-url is required")
    manifest = load_mcpb_manifest(args.bundle)
    payload = build_payload_from_manifest(manifest)
    if args.dry_run:
        output = args.payload_output or Path("var/test-logs/smithery-payload.json")
        _write_payload(output, payload)
        LOGGER.info("Smithery payload is ready: %s", output)
        return 0
    result = publish_bundle(
        bundle_path=args.bundle,
        qualified_name=args.name,
        base_url=args.base_url,
        settings_path=args.settings,
    )
    LOGGER.info("Smithery release accepted: %s", json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
