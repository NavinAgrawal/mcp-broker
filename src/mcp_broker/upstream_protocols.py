"""Structural contracts for the upstream clients the daemon registries hold.

The daemon keeps per-session/shared upstream clients in dicts and the call/list
mixin, the idle janitor, the health snapshots, and the unit-test fakes all operate
on those values. Naming the structural contract here lets the registries be typed
without coupling to the concrete StdioUpstreamProcess / HttpUpstreamClient classes
(and lets the test doubles satisfy the same contract by shape). These are typing
constructs only - not runtime-checkable, never isinstance'd.
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp_broker.config import UpstreamConfig


class StdioUpstreamClientProtocol(Protocol):
    """What the daemon does with a stdio upstream client held in the registry."""

    upstream: UpstreamConfig
    status: str

    def call_tool(
        self, tool_name: str, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]: ...

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, Any]]: ...

    def stop(self) -> tuple[int, ...]: ...

    def idle_seconds(self, *, now: float | None = None) -> float: ...

    def health_snapshot(self) -> dict[str, object]: ...

    def ensure_running(self) -> None: ...


class HttpUpstreamClientProtocol(Protocol):
    """What the daemon does with an HTTP upstream client held in the registry."""

    def call_tool(
        self, tool_name: str, arguments: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]: ...

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, Any]]: ...

    def health_snapshot(self) -> dict[str, object]: ...
