import sys
from pathlib import Path
from typing import cast

import pytest

from mcp_broker.config import (
    BrokerConfig,
    BrokerSettings,
    ResourcePolicy,
    RuntimeConfig,
    UpstreamConfig,
)
from mcp_broker.daemon import BrokerDaemon, StdioUpstreamClientProtocol
from mcp_broker.upstream_stdio import StdioUpstreamProcess


pytestmark = pytest.mark.journey


def _config(tmp_path: Path) -> BrokerConfig:
    return BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "ps": UpstreamConfig(
                name="ps",
                command=sys.executable,
                mode="per_session",
                tool_prefix="ps",
                resources=ResourcePolicy(idle_timeout_seconds=60),
            ),
            "ps0": UpstreamConfig(
                name="ps0",
                command=sys.executable,
                mode="per_session",
                tool_prefix="ps0",
                resources=ResourcePolicy(idle_timeout_seconds=0),
            ),
            "shared": UpstreamConfig(
                name="shared",
                command=sys.executable,
                mode="shared",
                tool_prefix="shared",
                resources=ResourcePolicy(idle_timeout_seconds=60),
            ),
        },
    )


def _daemon(tmp_path: Path) -> BrokerDaemon:
    config = _config(tmp_path)
    return BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )


def _client(daemon: BrokerDaemon, upstream_name: str, tmp_path: Path) -> StdioUpstreamProcess:
    assert daemon.broker_config is not None
    return StdioUpstreamProcess(
        daemon.broker_config.upstreams[upstream_name],
        runtime_state_dir=tmp_path / "rs",
    )


def test_reaps_idle_per_session_upstream(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    client = _client(daemon, "ps", tmp_path)
    client.record_activity(monotonic_seconds=0.0)
    daemon._stdio_upstreams[("ps", "s1")] = client

    reaped = daemon._reap_idle_upstreams(now=120.0)

    assert ("ps", "s1") not in daemon._stdio_upstreams
    assert [key for key, _client, _remaining in reaped] == [("ps", "s1")]


def test_keeps_recent_per_session_upstream(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    client = _client(daemon, "ps", tmp_path)
    client.record_activity(monotonic_seconds=100.0)
    daemon._stdio_upstreams[("ps", "s2")] = client

    daemon._reap_idle_upstreams(now=130.0)  # idle 30s < 60s timeout

    assert ("ps", "s2") in daemon._stdio_upstreams


def test_never_reaps_shared_upstream(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    client = _client(daemon, "shared", tmp_path)
    client.record_activity(monotonic_seconds=0.0)
    daemon._stdio_upstreams["shared"] = client

    daemon._reap_idle_upstreams(now=99999.0)

    assert "shared" in daemon._stdio_upstreams


def test_reaps_exited_shared_upstream(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)

    class ExitedSharedClient:
        upstream = daemon.broker_config.upstreams["shared"]  # type: ignore[union-attr]
        status = "exited"

        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> tuple[int, ...]:
            self.stop_calls += 1
            return ()

    client = ExitedSharedClient()
    daemon._stdio_upstreams["shared"] = cast(StdioUpstreamClientProtocol, client)

    reaped = daemon._reap_idle_upstreams(now=0.0)

    assert "shared" not in daemon._stdio_upstreams
    assert client.stop_calls == 1
    assert [key for key, _client, _remaining in reaped] == ["shared"]


def test_reaps_exited_shared_process_and_closes_its_pipes(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    client = _client(daemon, "shared", tmp_path)
    client.ensure_running()
    process = client._process
    assert process is not None
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.kill()
    process.wait(timeout=1)
    daemon._stdio_upstreams["shared"] = client

    daemon._reap_idle_upstreams(now=0.0)

    assert "shared" not in daemon._stdio_upstreams
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_idle_timeout_zero_disables_reaping(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    client = _client(daemon, "ps0", tmp_path)
    client.record_activity(monotonic_seconds=0.0)
    daemon._stdio_upstreams[("ps0", "s1")] = client

    daemon._reap_idle_upstreams(now=99999.0)

    assert ("ps0", "s1") in daemon._stdio_upstreams


def test_identity_guard_does_not_evict_a_replacement_client(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    key = ("ps", "s1")
    replacement = _client(daemon, "ps", tmp_path)

    class _ReplacingClient(StdioUpstreamProcess):
        def stop(self) -> tuple[int, ...]:
            # Simulate a concurrent session re-creating the upstream under the
            # same key during the stop-outside-lock window.
            daemon._stdio_upstreams[key] = replacement
            return ()

    doomed = _ReplacingClient(
        daemon.broker_config.upstreams["ps"],  # type: ignore[union-attr]
        runtime_state_dir=tmp_path / "rs",
    )
    doomed.record_activity(monotonic_seconds=0.0)
    daemon._stdio_upstreams[key] = doomed

    daemon._reap_idle_upstreams(now=120.0)

    assert daemon._stdio_upstreams[key] is replacement


def test_janitor_loop_runs_a_sweep_and_survives_reap_errors(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    sweeps: list[int] = []

    class _OneShotStop:
        def __init__(self) -> None:
            self._count = 0

        def wait(self, timeout: float) -> bool:
            # False on the first check (run one sweep), True afterwards (exit).
            self._count += 1
            return self._count > 1

        def set(self) -> None:
            self._count = 99

    def _boom(*, now: float | None = None) -> list[object]:
        sweeps.append(1)
        raise RuntimeError("reap blew up")

    daemon._janitor_stop = _OneShotStop()  # type: ignore[assignment]
    daemon._reap_idle_upstreams = _boom  # type: ignore[assignment, method-assign]

    # Must return (the loop swallows the reap error) rather than propagate or hang.
    daemon._idle_janitor_loop()

    assert sweeps == [1]
