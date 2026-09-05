import json
import os
import socket
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = Path(
    os.environ.get(
        "MCP_BROKER_LIVE_CONFIG_PATH",
        str(ROOT / "config" / "broker.private.yaml"),
    )
).expanduser()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "configured_upstream_name" not in metafunc.fixturenames:
        return
    names = _configured_upstream_names()
    if not names:
        metafunc.parametrize(
            "configured_upstream_name",
            [pytest.param("", marks=pytest.mark.skip("private config has no enabled upstreams"))],
        )
        return
    metafunc.parametrize("configured_upstream_name", names, ids=names)


def test_broker_centralizes_yaml_configured_live_upstreams(
    tmp_path: Path,
    configured_upstream_name: str,
) -> None:
    config = _private_config_or_skip()

    _exercise_configured_upstream(config, configured_upstream_name, tmp_path)


def test_broker_compacts_yaml_configured_profiles(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon

    config = _private_config_or_skip()
    compact_profiles = [
        profile
        for profile in config.profiles.values()
        if profile.compact_tools_enabled
        and any(
            upstream.enabled
            and upstream.mode != "disabled"
            and profile.name in upstream.profiles
            for upstream in config.upstreams.values()
        )
    ]

    if not compact_profiles:
        pytest.skip("private config has no compact profiles with enabled upstreams")

    runtime_root = tmp_path / "runtime-compact"
    socket_path = _socket_path()
    daemon_config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=config.broker,
        upstreams=config.upstreams,
        profiles=config.profiles,
        clients=config.clients,
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=daemon_config,
    )

    daemon.start()
    try:
        _initialize(socket_path)
        for profile in compact_profiles:
            response = _request(
                socket_path,
                {
                    "jsonrpc": "2.0",
                    "id": f"list-{profile.name}",
                    "method": "tools/list",
                    "params": {"profile": profile.name},
                },
            )
            tool_names = [tool["name"] for tool in response["result"]["tools"]]
            assert tool_names == [
                profile.exposed_broker_tool_name("broker.search_tools"),
                profile.exposed_broker_tool_name("broker.describe_tool"),
                profile.exposed_broker_tool_name("broker.call_tool"),
                profile.exposed_broker_tool_name("broker.status"),
                profile.exposed_broker_tool_name("broker.close_session"),
            ], profile.name
        _request(socket_path, {"method": "broker/stop", "id": "stop-compact"})
        daemon.join(timeout=2)
    finally:
        daemon.stop()


def _exercise_configured_upstream(
    config: object,
    upstream_name: str,
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon

    upstream = config.upstreams[upstream_name]
    runtime_root = tmp_path / f"runtime-{_safe_name(upstream_name)}"
    socket_path = _socket_path()
    daemon_config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=config.broker,
        upstreams={upstream_name: upstream},
        profiles=config.profiles,
        clients=config.clients,
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=daemon_config,
    )
    session_id = f"live-{uuid.uuid4().hex}"
    session_params = _session_params_for_upstream(upstream, tmp_path)

    daemon.start()
    try:
        _initialize(socket_path)
        list_response = _request(
            socket_path,
                {
                    "jsonrpc": "2.0",
                    "id": f"list-{upstream_name}",
                    "method": "tools/list",
                    "params": {"broker_session_id": session_id, **session_params},
                },
            )
        assert list_response["id"] == f"list-{upstream_name}"
        assert "result" in list_response, list_response
        tool_names = [tool["name"] for tool in list_response["result"]["tools"]]
        assert tool_names, upstream_name
        prefix = upstream.tool_prefix or upstream.name
        separator = config.broker.tool_namespace_separator
        assert all(name.startswith(f"{prefix}{separator}") for name in tool_names), upstream_name
        if upstream.smoke is not None and upstream.smoke.call:
            smoke_response = _request(
                socket_path,
                {
                    "jsonrpc": "2.0",
                    "id": f"smoke-{upstream_name}",
                    "method": "tools/call",
                    "params": {
                        "name": upstream.smoke.tool,
                        "arguments": dict(upstream.smoke.arguments),
                        "broker_session_id": session_id,
                        **session_params,
                    },
                },
            )
            assert smoke_response["id"] == f"smoke-{upstream_name}"
            assert "result" in smoke_response, smoke_response
        _request(socket_path, {"method": "broker/stop", "id": f"stop-{upstream_name}"})
        daemon.join(timeout=2)
    finally:
        daemon.stop()


def _private_config_or_skip() -> object:
    from mcp_broker.config import BrokerConfig

    if not CONFIG_FILE.exists():
        pytest.skip("private config is optional and ignored")
    return BrokerConfig.from_file(CONFIG_FILE)


def _session_params_for_upstream(upstream: object, tmp_path: Path) -> dict[str, str]:
    session_env = getattr(upstream, "session_env", {})
    if "client_cwd" not in session_env.values():
        return {}
    client_project = tmp_path / "client-project"
    client_project.mkdir(exist_ok=True)
    return {"broker_client_cwd": str(client_project)}


def _configured_upstream_names() -> list[str]:
    if not CONFIG_FILE.exists():
        return []
    config = _load_config_for_collection()
    return [
        upstream.name
        for upstream in config.upstreams.values()
        if upstream.enabled and upstream.mode != "disabled"
    ]


def _load_config_for_collection() -> object:
    from mcp_broker.config import BrokerConfig

    return BrokerConfig.from_file(CONFIG_FILE)


def _initialize(socket_path: Path) -> None:
    response = _request(
        socket_path,
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    )
    assert response["id"] == "initialize"
    assert "result" in response, response


def _request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        raw = b"".join(chunks)
    return json.loads(raw.decode("utf-8"))


def _socket_path() -> Path:
    return Path("/tmp") / f"mcp-broker-{uuid.uuid4().hex}.sock"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value)
