"""Upstream call and listing methods for the daemon."""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING

from mcp_broker.broker import BrokerToolError
from mcp_broker.daemon_helpers import result_matches_auth_repair, stdio_client_key
from mcp_broker.upstream_http import (
    HttpUpstreamError,
    HttpUpstreamTimeout,
)
from mcp_broker.upstream_stdio import (
    StdioUpstreamError,
    StdioUpstreamTimeout,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mcp_broker.config import BrokerConfig, UpstreamConfig
    from mcp_broker.runtime_reaper import RuntimePaths
    from mcp_broker.upstream_protocols import (
        HttpUpstreamClientProtocol,
        StdioUpstreamClientProtocol,
    )
    from mcp_broker.upstream_stdio import UpstreamEventLogger


class BrokerDaemonUpstreamMixin:
    # Host members provided by the concrete BrokerDaemon; declared for the type
    # checker only (never executed at runtime). The registries and factories are
    # typed against the upstream-client Protocols, so the concrete clients and the
    # unit-test fakes both satisfy the contract structurally.
    if TYPE_CHECKING:
        broker_config: BrokerConfig | None
        _stdio_upstreams: dict[str | tuple[str, str], StdioUpstreamClientProtocol]
        _active_per_call_upstreams: dict[
            str, tuple[str, StdioUpstreamClientProtocol]
        ]
        _upstreams_shutdown: bool
        _stdio_upstreams_lock: threading.Lock
        _http_upstreams: dict[str, HttpUpstreamClientProtocol]
        _paths: RuntimePaths

        def _create_stdio_upstream_process(
            self,
            upstream: UpstreamConfig,
            *,
            runtime_state_dir: Path,
            session_context: dict[str, str] | None = ...,
            event_logger: UpstreamEventLogger | None = ...,
            runtime_paths: RuntimePaths | None = ...,
            process_metadata_name: str | None = ...,
        ) -> StdioUpstreamClientProtocol: ...

        def _create_http_upstream_client(
            self, upstream: UpstreamConfig
        ) -> HttpUpstreamClientProtocol: ...

        def _write_upstream_event(
            self, event: str, upstream_name: str, fields: dict[str, object]
        ) -> None: ...

        def _record_auth_repair_attempt(self, upstream_name: str) -> None: ...

        def _record_auth_repair_success(self, upstream_name: str) -> None: ...

        def _record_auth_repair_failure(self, upstream_name: str) -> None: ...

    def _call_upstream(
        self,
        upstream_name: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
        *,
        session_id: str | None = None,
        session_context: dict[str, str] | None = None,
    ) -> dict[str, object]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        if upstream.transport in {"http", "sse"}:
            return self._call_http_upstream(
                upstream_name,
                tool_name,
                arguments,
                timeout_seconds,
            )
        return self._call_stdio_upstream(
            upstream_name,
            tool_name,
            arguments,
            timeout_seconds,
            session_id=session_id,
            session_context=session_context,
        )

    def _call_stdio_upstream(
        self,
        upstream_name: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
        *,
        session_id: str | None = None,
        session_context: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            return self._call_stdio_upstream_or_repair(
                upstream_name,
                tool_name,
                arguments,
                timeout_seconds,
                session_id=session_id,
                session_context=session_context,
            )
        except StdioUpstreamTimeout as exc:
            raise _broker_timeout(upstream_name, tool_name) from exc
        except StdioUpstreamError as exc:
            raise _broker_error(upstream_name, tool_name, str(exc)) from exc

    def _call_stdio_upstream_or_repair(
        self,
        upstream_name: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
        *,
        session_id: str | None,
        session_context: dict[str, str] | None,
    ) -> dict[str, object]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        per_call = upstream.mode == "per_call"
        if not per_call:
            client = self._stdio_client(
                upstream_name,
                session_id=session_id,
                session_context=session_context,
            )
            return self._call_stdio_client_or_repair(
                client,
                upstream_name,
                tool_name,
                arguments,
                timeout_seconds,
            )
        call_id, client = self._per_call_stdio_client(
            upstream_name,
            session_context=session_context,
        )
        try:
            result = self._call_stdio_client_or_repair(
                client,
                upstream_name,
                tool_name,
                arguments,
                timeout_seconds,
            )
        except BaseException:
            self._stop_per_call_stdio_client(
                call_id,
                upstream_name,
                client,
                preserve_active_error=True,
            )
            raise
        self._stop_per_call_stdio_client(
            call_id,
            upstream_name,
            client,
            preserve_active_error=False,
        )
        return result

    def _call_stdio_client_or_repair(
        self,
        client: "StdioUpstreamClientProtocol",
        upstream_name: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        result = client.call_tool(tool_name, arguments, timeout_seconds=timeout_seconds)
        if not result_matches_auth_repair(upstream, result):
            return result
        repair = upstream.auth_repair
        assert repair is not None
        self._record_auth_repair_attempt(upstream_name)
        try:
            repair_result = client.call_tool(
                repair.tool,
                repair.arguments or {},
                timeout_seconds=repair.timeout_seconds,
            )
            if not repair.retry_original:
                if result_matches_auth_repair(upstream, repair_result):
                    self._record_auth_repair_failure(upstream_name)
                    return repair_result
                self._record_auth_repair_success(upstream_name)
                return repair_result
            retry_result = client.call_tool(tool_name, arguments, timeout_seconds=timeout_seconds)
        except Exception:
            self._record_auth_repair_failure(upstream_name)
            raise
        if result_matches_auth_repair(upstream, retry_result):
            self._record_auth_repair_failure(upstream_name)
            return retry_result
        self._record_auth_repair_success(upstream_name)
        return retry_result

    def _list_stdio_upstream(
        self,
        upstream_name: str,
        timeout_seconds: int,
        *,
        session_id: str | None = None,
        session_context: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        if upstream.mode != "per_call":
            client = self._stdio_client(
                upstream_name,
                session_id=session_id,
                session_context=session_context,
            )
            return client.list_tools(timeout_seconds=timeout_seconds)
        call_id, client = self._per_call_stdio_client(
            upstream_name,
            session_context=session_context,
        )
        try:
            result = client.list_tools(timeout_seconds=timeout_seconds)
        except BaseException:
            self._stop_per_call_stdio_client(
                call_id,
                upstream_name,
                client,
                preserve_active_error=True,
            )
            raise
        self._stop_per_call_stdio_client(
            call_id,
            upstream_name,
            client,
            preserve_active_error=False,
        )
        return result

    def _per_call_stdio_client(
        self,
        upstream_name: str,
        *,
        session_context: dict[str, str] | None,
    ) -> tuple[str, "StdioUpstreamClientProtocol"]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        if upstream.session_env:
            upstream.resolve_session_environment(session_context or {})
        call_id = uuid.uuid4().hex
        with self._stdio_upstreams_lock:
            if self._upstreams_shutdown:
                raise StdioUpstreamError("broker upstreams are shutting down")
            client = self._create_stdio_upstream_process(
                upstream,
                runtime_state_dir=self.broker_config.runtime.state_dir,
                session_context=session_context,
                event_logger=self._write_upstream_event,
                runtime_paths=self._paths,
                process_metadata_name=f"{upstream.name}.call.{call_id}",
            )
            self._active_per_call_upstreams[call_id] = (upstream_name, client)
        return call_id, client

    def _stop_per_call_stdio_client(
        self,
        call_id: str,
        upstream_name: str,
        client: "StdioUpstreamClientProtocol",
        *,
        preserve_active_error: bool,
    ) -> None:
        if type(preserve_active_error) is not bool:
            raise TypeError("preserve_active_error must be a bool")
        try:
            with self._stdio_upstreams_lock:
                registered = self._active_per_call_upstreams.get(call_id)
                if registered != (upstream_name, client):
                    return
                remaining_processes = client.stop()
                self._active_per_call_upstreams.pop(call_id)
        except Exception as exc:
            cleanup_error = StdioUpstreamError(
                f"upstream cleanup failed: {upstream_name}: {exc}"
            )
            try:
                self._write_upstream_event(
                    "upstream.stop_error",
                    upstream_name,
                    {"error": str(exc)},
                )
            except Exception:
                if not preserve_active_error:
                    raise cleanup_error from exc
                return
            if not preserve_active_error:
                raise cleanup_error from exc
            return
        if remaining_processes:
            try:
                self._write_upstream_event(
                    "upstream.stop_incomplete",
                    upstream_name,
                    {"remaining_broker_processes": sorted(set(remaining_processes))},
                )
            except Exception:
                return

    def _stdio_client(
        self,
        upstream_name: str,
        *,
        session_id: str | None,
        session_context: dict[str, str] | None = None,
    ) -> "StdioUpstreamClientProtocol":
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        if upstream.session_env:
            upstream.resolve_session_environment(session_context or {})
        client_key = stdio_client_key(upstream, session_id=session_id)
        # Atomic get-or-create under the registry lock: prevents two connection
        # threads double-creating the same key, and prevents the idle janitor from
        # evicting a key mid-creation. Construction does not spawn the subprocess
        # (that happens lazily on first call), so the lock is held only briefly.
        with self._stdio_upstreams_lock:
            if self._upstreams_shutdown:
                raise StdioUpstreamError("broker upstreams are shutting down")
            client = self._stdio_upstreams.get(client_key)
            if client is None:
                client = self._create_stdio_upstream_process(
                    upstream,
                    runtime_state_dir=self.broker_config.runtime.state_dir,
                    session_context=session_context,
                    event_logger=self._write_upstream_event,
                    runtime_paths=self._paths,
                )
                self._stdio_upstreams[client_key] = client
            return client

    def _call_http_upstream(
        self,
        upstream_name: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        self._log_http_call(upstream_name, tool_name, timeout_seconds)
        try:
            return self._http_client(upstream_name).call_tool(
                tool_name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
        except HttpUpstreamTimeout as exc:
            self._log_http_timeout(upstream_name, tool_name, timeout_seconds)
            raise _broker_timeout(upstream_name, tool_name) from exc
        except HttpUpstreamError as exc:
            raise _broker_error(upstream_name, tool_name, str(exc)) from exc

    def _list_upstream(
        self,
        upstream_name: str,
        timeout_seconds: int,
        *,
        session_id: str | None = None,
        session_context: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        if upstream.transport in {"http", "sse"}:
            self._write_upstream_event(
                "upstream.call",
                upstream_name,
                {"method": "tools/list", "timeout_seconds": timeout_seconds},
            )
            return self._http_client(upstream_name).list_tools(timeout_seconds=timeout_seconds)
        return self._list_stdio_upstream(
            upstream_name,
            timeout_seconds,
            session_id=session_id,
            session_context=session_context,
        )

    def _http_client(self, upstream_name: str) -> "HttpUpstreamClientProtocol":
        assert self.broker_config is not None
        upstream = self.broker_config.upstreams[upstream_name]
        client = self._http_upstreams.get(upstream_name)
        if client is None:
            client = self._create_http_upstream_client(upstream)
            self._http_upstreams[upstream_name] = client
        return client

    def _call_upstream_for_session(
        self,
        session_id: str | None,
        session_context: dict[str, str] | None,
    ):
        def call_upstream(
            upstream_name: str,
            tool_name: str,
            arguments: dict[str, object],
            timeout_seconds: int,
        ) -> dict[str, object]:
            return self._call_upstream(
                upstream_name,
                tool_name,
                arguments,
                timeout_seconds,
                session_id=session_id,
                session_context=session_context,
            )

        return call_upstream

    def _list_upstream_for_session(
        self,
        session_id: str | None,
        session_context: dict[str, str] | None,
    ):
        def list_upstream(upstream_name: str, timeout_seconds: int) -> list[dict[str, object]]:
            return self._list_upstream(
                upstream_name,
                timeout_seconds,
                session_id=session_id,
                session_context=session_context,
            )

        return list_upstream

    def _log_http_call(self, upstream_name: str, tool_name: str, timeout_seconds: int) -> None:
        self._write_upstream_event(
            "upstream.call",
            upstream_name,
            {
                "method": "tools/call",
                "tool_name": tool_name,
                "timeout_seconds": timeout_seconds,
            },
        )

    def _log_http_timeout(self, upstream_name: str, tool_name: str, timeout_seconds: int) -> None:
        self._write_upstream_event(
            "upstream.timeout",
            upstream_name,
            {
                "method": "tools/call",
                "tool_name": tool_name,
                "timeout_seconds": timeout_seconds,
            },
        )


def _broker_timeout(upstream_name: str, tool_name: str) -> BrokerToolError:
    return BrokerToolError(
        code="upstream_timeout",
        message=f"upstream timed out: {upstream_name}",
        upstream_name=upstream_name,
        tool_name=tool_name,
    )


def _broker_error(upstream_name: str, tool_name: str, message: str) -> BrokerToolError:
    return BrokerToolError(
        code="upstream_error",
        message=message,
        upstream_name=upstream_name,
        tool_name=tool_name,
    )
