"""Transport clients and daemon harness shared by upstream contract tests."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
from mcp_broker.daemon_upstreams import BrokerDaemonUpstreamMixin
from mcp_broker.runtime_reaper import RuntimePaths
from mcp_broker.schema import AuthRepairPolicy
from mcp_broker.upstream_protocols import HttpUpstreamClientProtocol, StdioUpstreamClientProtocol


class UpstreamHarness(BrokerDaemonUpstreamMixin):
    def __init__(self, tmp_path: Path, upstreams: dict[str, UpstreamConfig]) -> None:
        runtime = RuntimeConfig(
            root=tmp_path,
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state",
            secrets_dir=tmp_path / "secrets",
        )
        self.broker_config = BrokerConfig(
            runtime=runtime,
            broker=BrokerSettings(),
            upstreams=upstreams,
        )
        self._paths = RuntimePaths.from_root(tmp_path)
        self._stdio_upstreams: dict[str | tuple[str, str], StdioUpstreamClientProtocol] = {}
        self._active_per_call_upstreams: dict[
            str, tuple[str, StdioUpstreamClientProtocol]
        ] = {}
        self._stdio_upstreams_lock = threading.Lock()
        self._upstreams_shutdown = False
        self._http_upstreams: dict[str, HttpUpstreamClientProtocol] = {}
        self.stdio_clients_to_create: list[FakeStdioClient] = []
        self.http_clients_to_create: list[FakeHttpClient] = []
        self.stdio_creates: list[dict[str, object]] = []
        self.http_creates: list[str] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.auth_repair_events: list[tuple[str, str]] = []
        self.create_lock_states: list[bool] = []
        self.event_exception: Exception | None = None

    def _create_stdio_upstream_process(
        self,
        upstream: UpstreamConfig,
        **kwargs: object,
    ) -> FakeStdioClient:
        acquired = self._stdio_upstreams_lock.acquire(blocking=False)
        self.create_lock_states.append(not acquired)
        if acquired:
            self._stdio_upstreams_lock.release()
        self.stdio_creates.append({"upstream": upstream.name} | kwargs)
        return self.stdio_clients_to_create.pop(0)

    def _create_http_upstream_client(self, upstream: UpstreamConfig) -> FakeHttpClient:
        self.http_creates.append(upstream.name)
        return self.http_clients_to_create.pop(0)

    def _write_upstream_event(
        self,
        event: str,
        upstream_name: str,
        fields: dict[str, object],
    ) -> None:
        if self.event_exception is not None:
            raise self.event_exception
        self.events.append((event, upstream_name, fields))

    def _record_auth_repair_attempt(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("attempt", upstream_name))

    def _record_auth_repair_success(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("success", upstream_name))

    def _record_auth_repair_failure(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("failure", upstream_name))


class FakeStdioClient:
    def __init__(
        self,
        *,
        call_result: dict[str, object] | None = None,
        call_results: list[dict[str, object]] | None = None,
        call_exception: Exception | None = None,
        list_result: list[dict[str, object]] | None = None,
        list_exception: Exception | None = None,
        stop_result: tuple[int, ...] = (),
        stop_exception: Exception | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.call_result: dict[str, object] = (
            {"content": []} if call_result is None else call_result
        )
        self.call_results = [] if call_results is None else call_results
        self.call_exception = call_exception
        self.list_result = [] if list_result is None else list_result
        self.list_exception = list_exception
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.lists: list[int] = []
        self.stop_calls = 0
        self.stop_result = stop_result
        self.stop_exception = stop_exception
        self.on_call = on_call
        # Satisfies StdioUpstreamClientProtocol; tests do not exercise these.
        self.upstream = UpstreamConfig(name="fake", command="fake")

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append((tool_name, arguments, timeout_seconds))
        if self.on_call is not None:
            self.on_call()
        if self.call_exception is not None:
            raise self.call_exception
        if self.call_results:
            return self.call_results.pop(0)
        return self.call_result

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.lists.append(timeout_seconds)
        if self.list_exception is not None:
            raise self.list_exception
        return self.list_result

    def stop(self) -> tuple[int, ...]:
        self.stop_calls += 1
        if self.stop_exception is not None:
            raise self.stop_exception
        return self.stop_result

    def idle_seconds(self, *, now: float | None = None) -> float:
        return 0.0

    def health_snapshot(self) -> dict[str, object]:
        return {"state": "running"}

    def ensure_running(self) -> None:
        return None


class FakeHttpClient:
    def __init__(
        self,
        *,
        call_result: dict[str, object] | None = None,
        call_exception: Exception | None = None,
        list_result: list[dict[str, object]] | None = None,
    ) -> None:
        self.call_result: dict[str, object] = (
            {"content": []} if call_result is None else call_result
        )
        self.call_exception = call_exception
        self.list_result = [] if list_result is None else list_result
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.lists: list[int] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append((tool_name, arguments, timeout_seconds))
        if self.call_exception is not None:
            raise self.call_exception
        return self.call_result

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.lists.append(timeout_seconds)
        return self.list_result

    def health_snapshot(self) -> dict[str, object]:
        return {"state": "running"}


def _upstream(
    name: str,
    *,
    mode: str = "shared",
    transport: str = "stdio",
    session_env: dict[str, str] | None = None,
    auth_repair: AuthRepairPolicy | None = None,
) -> UpstreamConfig:
    return UpstreamConfig(
        name=name,
        command="/bin/echo",
        mode=mode,
        transport=transport,
        session_env={} if session_env is None else session_env,
        auth_repair=auth_repair,
    )


def _error_result(message: str) -> dict[str, object]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": f"Error: {message}"}],
    }
