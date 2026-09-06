"""Direct contracts for the per-call health projection."""

from pathlib import Path

import pytest

from mcp_broker import daemon_helpers
from tests.support.per_call_status import configured_daemon


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("owners, expected_count", [([], 0), (["other-task"], 0), (["task"], 1), (["task", "other-task", "task"], 2)])
def test_per_call_projection_counts_only_matching_owners(
    tmp_path: Path, owners: list[str], expected_count: int,
) -> None:
    daemon = configured_daemon(tmp_path)
    upstream = daemon.broker_config.upstreams["task"]
    clients = [daemon._per_call_stdio_client(name, session_context=None) for name in owners]
    try:
        registered = tuple(daemon._active_per_call_upstreams.values())
        snapshot = daemon_helpers.per_call_health_snapshot(upstream, registered)

        assert snapshot == {
            "state": "running" if expected_count else "configured",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": None,
            "active_call_count": expected_count,
        }
        assert tuple(daemon._active_per_call_upstreams.values()) == registered
        assert all(client.pid is None for _, client in clients)
        assert not daemon._paths.upstream_pid_dir.exists()
    finally:
        for owner, (call_id, client) in zip(owners, clients, strict=True):
            daemon._stop_per_call_stdio_client(call_id, owner, client, preserve_active_error=False)


def test_per_call_projection_preserves_disabled_idle_health_and_returns_fresh_data(tmp_path: Path) -> None:
    from dataclasses import replace

    daemon = configured_daemon(tmp_path)
    upstream = replace(daemon.broker_config.upstreams["task"], enabled=False)
    first = daemon_helpers.per_call_health_snapshot(upstream, ())
    first["active_call_count"] = 99

    assert daemon_helpers.per_call_health_snapshot(upstream, ()) == {
        "state": "disabled",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "active_call_count": 0,
    }
