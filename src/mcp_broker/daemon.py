"""Unix socket daemon for the local mcp-broker process."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Sequence

from mcp_broker import __version__
from mcp_broker.broker import BrokerCore, BrokerToolError
from mcp_broker.catalog import profile_allows_upstream
from mcp_broker.config import BrokerConfig, UpstreamConfig
from mcp_broker.daemon_helpers import (
    configured_upstream_health as _configured_upstream_health,
    merge_passive_auth_probe as _merge_passive_auth_probe,
    passive_auth_probe as _passive_auth_probe,
    per_call_health_snapshot as _per_call_health_snapshot,
    per_session_health_snapshot as _per_session_health_snapshot,
    process_exists as _process_exists,
    _result_content_text,
    result_matches_auth_repair as _result_matches_auth_repair,
    stdio_client_name as _stdio_client_name,
    utc_timestamp as _utc_timestamp,
)
from mcp_broker.daemon_errors import BrokerDaemonError, BrokerRequestTooLarge
from mcp_broker.daemon_provenance import git_sha as _git_sha_impl
from mcp_broker.daemon_provenance import source_provenance as _source_provenance_impl
from mcp_broker.daemon_request_context import BrokerDaemonRequestContextMixin
from mcp_broker.daemon_status import BrokerDaemonStatusMixin
from mcp_broker.daemon_tool_calls import BrokerDaemonToolCallMixin
from mcp_broker.daemon_upstreams import BrokerDaemonUpstreamMixin
from mcp_broker.jsonrpc import JsonRpcRequest, JsonRpcResponse
from mcp_broker.protocol import McpProtocolHandler
from mcp_broker.runtime_reaper import RuntimePaths, write_socket_metadata
from mcp_broker.shared_worker import SharedWorkerRuntime
from mcp_broker.schema import DEFAULT_SOCKET_MAX_REQUEST_BYTES, DEFAULT_SOCKET_READ_TIMEOUT_SECONDS
from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError
from mcp_broker.upstream_protocols import (
    HttpUpstreamClientProtocol,
    StdioUpstreamClientProtocol,
)
from mcp_broker.upstream_stdio import (
    StdioUpstreamError,
    StdioUpstreamProcess,
    UpstreamEventLogger,
)


# Fixed cadence for the idle-upstream janitor sweep. Deliberately NOT derived
# from per-upstream idle_timeout_seconds - a small configured timeout would turn
# a derived cadence into a hot loop. Each client is compared against its own
# idle_timeout_seconds; the worst case is an upstream living one interval past
# its timeout, which is irrelevant against the FD leak this reaper closes.
JANITOR_SWEEP_INTERVAL_SECONDS = 30
SOCKET_READ_CHUNK_BYTES = 65_536


def _git_sha(source_dir: Path) -> str | None:
    return _git_sha_impl(source_dir)


def _source_provenance() -> dict[str, str]:
    return _source_provenance_impl(
        Path(__file__).resolve().parent,
        __version__,
        git_sha_fn=_git_sha,
    )


@dataclass
class BrokerDaemon(
    BrokerDaemonStatusMixin,
    BrokerDaemonUpstreamMixin,
    BrokerDaemonRequestContextMixin,
    BrokerDaemonToolCallMixin,
):
    runtime_root: Path
    socket_path: Path
    broker_config: BrokerConfig | None = None

    def __post_init__(self) -> None:
        self._paths = RuntimePaths.from_root(self.runtime_root)
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connection_threads: list[threading.Thread] = []
        self._connection_threads_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._protocol = McpProtocolHandler(server_name="mcp-broker", server_version=__version__)
        self._stdio_upstreams: dict[str | tuple[str, str], StdioUpstreamClientProtocol] = {}
        self._active_per_call_upstreams: dict[
            str, tuple[str, StdioUpstreamClientProtocol]
        ] = {}
        self._upstreams_shutdown = False
        # Guards every mutation/iteration of _stdio_upstreams (insert in
        # _stdio_client, pop in _shutdown_session_upstreams, clear in
        # _shutdown_upstreams, the health read, and the idle janitor sweep).
        self._stdio_upstreams_lock = threading.Lock()
        self._janitor_thread: threading.Thread | None = None
        self._janitor_stop = threading.Event()
        self._http_upstreams: dict[str, HttpUpstreamClientProtocol] = {}
        self._upstream_call_locks: dict[str, threading.Lock] = {}
        self._cleanup_lock = threading.Lock()
        self._cleanup_done = False
        self._log_lock = threading.Lock()
        self._status_snapshot_lock = threading.Lock()
        self._stop_logged = False
        self._started_at: str | None = None
        broker_settings = self.broker_config.broker if self.broker_config is not None else None
        self._socket_read_timeout_seconds = (
            broker_settings.socket_read_timeout_seconds
            if broker_settings is not None
            else DEFAULT_SOCKET_READ_TIMEOUT_SECONDS
        )
        self._socket_max_request_bytes = (
            broker_settings.socket_max_request_bytes
            if broker_settings is not None
            else DEFAULT_SOCKET_MAX_REQUEST_BYTES
        )
        self._requests_total = 0
        self._request_errors_total = 0
        self._last_request_method: str | None = None
        self._last_request_status: str | None = None
        self._auth_repair_stats: dict[str, dict[str, int | str]] = {}
        self._shared_worker_runtime = SharedWorkerRuntime(tools=[])

    @property
    def lock_path(self) -> Path:
        return self._paths.run_dir / "broker.lock"

    def start(self) -> None:
        self._paths.ensure()
        self._acquire_lock()
        self._started_at = _utc_timestamp()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            server.listen()
        except OSError:
            self._release_lock()
            server.close()
            raise
        self._server = server
        write_socket_metadata(
            self._paths,
            socket_name=self.socket_path.name,
            pid=os.getpid(),
            broker_pid=os.getpid(),
        )
        self._write_log(
            "daemon.started",
            runtime_root=str(self.runtime_root),
            socket_path=str(self.socket_path),
            **_source_provenance(),
        )
        self._write_status_snapshot("running")
        self._thread = threading.Thread(target=self._serve_loop, name="mcp-broker-daemon")
        self._thread.start()
        self._janitor_stop.clear()
        self._janitor_thread = threading.Thread(
            target=self._idle_janitor_loop, name="mcp-broker-janitor", daemon=True
        )
        self._janitor_thread.start()

    def serve_forever(self) -> None:
        self.start()
        self._stop_requested.wait()
        self.join(timeout=5)

    def stop(self) -> None:
        self._stop_requested.set()
        self._wake_server()
        self.join(timeout=5)

    def join(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        self._janitor_stop.set()
        janitor = self._janitor_thread
        if janitor is not None:
            janitor.join(timeout=max(0.0, deadline - time.monotonic()))
            self._janitor_thread = None
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._join_connection_threads(max(0.0, deadline - time.monotonic()))
        self._cleanup()

    def _join_connection_threads(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._connection_threads_lock:
                threads = [thread for thread in self._connection_threads if thread.is_alive()]
                self._connection_threads = threads
            if not threads:
                return
            for thread in threads:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    return
                thread.join(timeout=remaining)

    def _serve_loop(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stop_requested.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                break
            self._start_connection_thread(connection)
        self._join_connection_threads(5)
        self._cleanup()

    def _start_connection_thread(self, connection: socket.socket) -> None:
        thread = threading.Thread(
            target=self._handle_connection_with_context,
            args=(connection,),
            name="mcp-broker-connection",
        )
        thread.daemon = True
        with self._connection_threads_lock:
            self._connection_threads = [item for item in self._connection_threads if item.is_alive()]
            self._connection_threads.append(thread)
        thread.start()

    def _handle_connection_with_context(self, connection: socket.socket) -> None:
        connection.settimeout(self._socket_read_timeout_seconds)
        with connection:
            self._handle_connection(connection)

    def _handle_connection(self, connection: socket.socket) -> None:
        try:
            raw = self._read_request(connection)
        except socket.timeout:
            return
        except BrokerRequestTooLarge as exc:
            response = JsonRpcResponse.error(None, -32600, str(exc)).to_mapping()
            self._send_response(connection, response)
            self._write_request_log_safely(None, None, response)
            return
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8").strip())
        except json.JSONDecodeError:
            response = JsonRpcResponse.error(None, -32700, "Parse error").to_mapping()
            self._send_response(connection, response)
            self._write_request_log_safely(None, None, response)
        else:
            response = self._handle_request(request)
            if response is not None:
                self._send_response(connection, response)
            self._write_request_log_safely(request.get("id"), request.get("method"), response)
            if request.get("method") == "broker/stop":
                self._wake_server()

    def _read_request(self, connection: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = connection.recv(SOCKET_READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > self._socket_max_request_bytes:
                raise BrokerRequestTooLarge(
                    f"Request exceeds {self._socket_max_request_bytes} bytes"
                )
            if chunk.endswith(b"\n"):
                break
        return b"".join(chunks)

    def _send_response(self, connection: socket.socket, response: dict[str, object]) -> None:
        try:
            connection.sendall(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_request(self, request: dict[str, object]) -> dict[str, object] | None:
        request_id = request.get("id")
        method = request.get("method")
        if method == "broker/health":
            return {
                "id": request_id,
                "result": self._health_result(request),
            }
        if method == "broker/stop":
            self._stop_requested.set()
            shutdown = self._shutdown_upstreams()
            return {
                "id": request_id,
                "result": {"stopping": True} | shutdown,
            }
        if method == "broker/session/stop":
            try:
                session_id = self._session_id_from_params(request.get("params"))
            except ValueError as exc:
                return {"id": request_id, "error": {"code": "invalid_params", "message": str(exc)}}
            if session_id is None:
                return {
                    "id": request_id,
                    "error": {
                        "code": "invalid_params",
                        "message": "broker_session_id is required",
                    },
                }
            return {
                "id": request_id,
                "result": self._shutdown_session_upstreams(session_id),
            }
        if request.get("jsonrpc") == "2.0":
            return self._handle_jsonrpc_request(request)
        return {"id": request_id, "error": {"code": "unknown_method"}}

    def _handle_jsonrpc_request(self, request: dict[str, object]) -> dict[str, object] | None:
        try:
            parsed = JsonRpcRequest.from_mapping(request)
        except ValueError as exc:
            return JsonRpcResponse.error(None, -32600, str(exc)).to_mapping()
        if parsed.method == "tools/list":
            if not self._protocol._initialize_seen:
                return JsonRpcResponse.error(parsed.id, -32002, "Server not initialized").to_mapping()
            return self._handle_tools_list(parsed).to_mapping()
        if parsed.method == "tools/call":
            return self._handle_tools_call(parsed).to_mapping()
        response = self._protocol.handle(parsed)
        if response is None:
            return None
        return response.to_mapping()

    def _handle_tools_list(self, request: JsonRpcRequest) -> JsonRpcResponse:
        if self.broker_config is None:
            return JsonRpcResponse.error(request.id, -32000, "broker config is not loaded")
        try:
            session_id = self._session_id_from_params(request.params)
            session_context = self._session_context_from_params(request.params)
            profile = self._effective_profile(request.params, session_context)
        except ValueError as exc:
            return JsonRpcResponse.error(request.id, -32602, str(exc))
        core = BrokerCore(
            settings=self.broker_config.broker,
            upstreams=self.broker_config.upstreams,
            profile=profile,
        )
        if profile is not None and profile.compact_tools_enabled:
            return JsonRpcResponse.result(request.id, core.compact_tools())
        try:
            upstream_tools = {}
            for name, upstream in self.broker_config.upstreams.items():
                if not upstream.enabled or upstream.mode == "disabled":
                    continue
                if not profile_allows_upstream(profile, upstream):
                    continue
                if (
                    profile is not None
                    and upstream.mutating
                    and not profile.allows_mutating_upstream(name)
                ):
                    raise ValueError(f"mutating upstream not allowed for profile: {name}")
                upstream_tools[name] = self._list_upstream(
                    name,
                    upstream.health.call_timeout_seconds,
                    session_id=session_id,
                    session_context=session_context,
                )
            result = core.list_tools(upstream_tools)
        except (BrokerToolError, HttpUpstreamError, StdioUpstreamError, ValueError) as exc:
            return JsonRpcResponse.error(request.id, -32000, str(exc))
        return JsonRpcResponse.result(request.id, result)

    def _create_stdio_upstream_process(
        self,
        upstream: UpstreamConfig,
        *,
        runtime_state_dir: Path,
        session_context: dict[str, str] | None = None,
        event_logger: UpstreamEventLogger | None = None,
        runtime_paths: RuntimePaths | None = None,
        process_metadata_name: str | None = None,
    ) -> StdioUpstreamClientProtocol:
        return StdioUpstreamProcess(
            upstream,
            runtime_state_dir=runtime_state_dir,
            session_context=session_context,
            event_logger=event_logger,
            runtime_paths=runtime_paths,
            process_metadata_name=process_metadata_name,
        )

    def _create_http_upstream_client(self, upstream: UpstreamConfig) -> HttpUpstreamClientProtocol:
        return HttpUpstreamClient(upstream)

    def _health_result(self, request: dict[str, object]) -> dict[str, object]:
        upstreams = self._upstream_health()
        return {
            "pid": os.getpid(),
            "socket_path": str(self.socket_path),
            "status": self._health_status(upstreams),
            "profile": self._effective_profile_name(request),
            "upstreams": upstreams,
        }

    def _health_status(self, upstreams: dict[str, dict[str, object]]) -> str:
        for snapshot in upstreams.values():
            state = snapshot.get("state")
            if state in {"exited", "failed", "backoff"} or snapshot.get("last_error"):
                return "degraded"
        return "ok"

    def _upstream_health_for_status(
        self,
        visible_upstreams: set[str] | None,
    ) -> dict[str, dict[str, object]]:
        return self._upstream_health(restart_upstreams=visible_upstreams)

    def _upstream_health(
        self,
        *,
        restart_upstreams: set[str] | None = None,
    ) -> dict[str, dict[str, object]]:
        if self.broker_config is None:
            return {}
        with self._stdio_upstreams_lock:
            stdio_snapshot = dict(self._stdio_upstreams)
            per_call_snapshot = tuple(self._active_per_call_upstreams.values())
        snapshots: dict[str, dict[str, object]] = {}
        for name, upstream in sorted(self.broker_config.upstreams.items()):
            client = stdio_snapshot.get(name)
            if client is not None:
                snapshots[name] = self._upstream_health_with_auth(
                    name,
                    self._shared_stdio_health_snapshot(
                        name,
                        client,
                        restart_allowed=restart_upstreams is None or name in restart_upstreams,
                    ),
                )
            elif upstream.mode == "per_call":
                snapshot = _per_call_health_snapshot(upstream, per_call_snapshot)
                snapshots[name] = self._upstream_health_with_auth(name, snapshot)
            elif upstream.mode == "per_session":
                session_clients = [
                    active_client
                    for key, active_client in stdio_snapshot.items()
                    if isinstance(key, tuple) and key[0] == name
                ]
                snapshot = (
                    _per_session_health_snapshot(session_clients)
                    if session_clients
                    else _configured_upstream_health(upstream)
                )
                snapshots[name] = self._upstream_health_with_auth(name, snapshot)
            elif name in self._http_upstreams:
                snapshots[name] = self._upstream_health_with_auth(
                    name,
                    self._http_upstreams[name].health_snapshot(),
                )
            else:
                snapshots[name] = self._upstream_health_with_auth(
                    name,
                    _configured_upstream_health(upstream),
                )
        return snapshots

    def _shared_stdio_health_snapshot(
        self,
        upstream_name: str,
        client: StdioUpstreamClientProtocol,
        *,
        restart_allowed: bool,
    ) -> dict[str, object]:
        snapshot = client.health_snapshot()
        if snapshot.get("state") != "exited" or not restart_allowed:
            return snapshot
        try:
            client.ensure_running()
        except StdioUpstreamError as exc:
            self._write_upstream_event(
                "upstream.backoff",
                upstream_name,
                {"state": "backoff", "error": str(exc)},
            )
            return snapshot | {"state": "backoff", "last_error": str(exc)}
        return client.health_snapshot()

    def _record_auth_repair_attempt(self, upstream_name: str) -> None:
        stats = self._auth_repair_stats_for(upstream_name)
        stats["auth_repair_attempts"] = int(stats["auth_repair_attempts"]) + 1
        stats["auth_state"] = "unauthenticated"

    def _record_auth_repair_success(self, upstream_name: str) -> None:
        stats = self._auth_repair_stats_for(upstream_name)
        stats["auth_repair_successes"] = int(stats["auth_repair_successes"]) + 1
        stats["auth_state"] = "authenticated"

    def _record_auth_repair_failure(self, upstream_name: str) -> None:
        stats = self._auth_repair_stats_for(upstream_name)
        stats["auth_repair_failures"] = int(stats["auth_repair_failures"]) + 1
        stats["auth_state"] = "unauthenticated"

    def _auth_repair_stats_for(self, upstream_name: str) -> dict[str, int | str]:
        return self._auth_repair_stats.setdefault(
            upstream_name,
            {
                "auth_repair_attempts": 0,
                "auth_repair_successes": 0,
                "auth_repair_failures": 0,
                "auth_state": "unknown",
            },
        )

    def _upstream_health_with_auth(
        self,
        upstream_name: str,
        snapshot: dict[str, object],
    ) -> dict[str, object]:
        if self.broker_config is not None and upstream_name in self.broker_config.upstreams:
            probe = _passive_auth_probe(
                self.broker_config.upstreams[upstream_name],
                environ=os.environ,
            )
            snapshot = _merge_passive_auth_probe(snapshot, probe)
        stats = self._auth_repair_stats.get(upstream_name)
        if stats is None:
            return snapshot
        return snapshot | stats

    def _shutdown_upstreams(self) -> dict[str, object]:
        stopped_upstreams: list[str] = []
        remaining_broker_processes: list[int] = []
        with self._stdio_upstreams_lock:
            self._upstreams_shutdown = True
            clients = sorted(self._stdio_upstreams.items(), key=lambda item: str(item[0]))
            self._stdio_upstreams.clear()
            active_per_call_clients = sorted(self._active_per_call_upstreams.items())
            self._active_per_call_upstreams.clear()
        for key, client in clients:
            remaining_broker_processes.extend(client.stop())
            stopped_upstreams.append(_stdio_client_name(key))
        for call_id, (upstream_name, client) in active_per_call_clients:
            remaining_broker_processes.extend(client.stop())
            stopped_upstreams.append(f"{upstream_name}:call:{call_id}")
        self._http_upstreams.clear()
        return {
            "stopped_upstreams": stopped_upstreams,
            "remaining_broker_processes": sorted(set(remaining_broker_processes)),
        }

    def _shutdown_session_upstreams(self, session_id: str) -> dict[str, object]:
        stopped_upstreams: list[str] = []
        remaining_broker_processes: list[int] = []
        with self._stdio_upstreams_lock:
            keys = sorted(
                (
                    key
                    for key in self._stdio_upstreams
                    if isinstance(key, tuple) and key[1] == session_id
                ),
                key=str,
            )
            clients = [(key, self._stdio_upstreams.pop(key)) for key in keys]
        for key, client in clients:
            remaining_broker_processes.extend(client.stop())
            stopped_upstreams.append(_stdio_client_name(key))
        return {
            "stopped_upstreams": stopped_upstreams,
            "remaining_broker_processes": sorted(set(remaining_broker_processes)),
        }

    def _idle_janitor_loop(self) -> None:
        while not self._janitor_stop.wait(JANITOR_SWEEP_INTERVAL_SECONDS):
            try:
                self._reap_idle_upstreams()
            except Exception as exc:  # never let the janitor thread die
                self._write_log("upstream.reap_error", error=str(exc))

    def _reap_idle_upstreams(
        self,
        *,
        now: float | None = None,
    ) -> list[tuple[str | tuple[str, str], StdioUpstreamClientProtocol, tuple[int, ...]]]:
        """Stop and evict dead stdio clients or idle per-session clients.

        Shared upstreams remain long-lived while running, but every exited client
        is reaped so its stdio pipes do not remain open until daemon shutdown.
        Snapshot under the registry lock, stop OUTSIDE it (stop() SIGKILLs a
        process group and can be slow), then re-acquire to pop - but only the exact
        client we stopped, so a session that re-created the upstream under the same
        key during the stop window is not evicted.
        """
        current = time.monotonic() if now is None else now
        with self._stdio_upstreams_lock:
            snapshot = list(self._stdio_upstreams.items())
        reaped: list[
            tuple[str | tuple[str, str], StdioUpstreamClientProtocol, tuple[int, ...]]
        ] = []
        reaped_events: list[tuple[str | tuple[str, str], tuple[int, ...], str]] = []
        for key, client in snapshot:
            exited = client.status == "exited"
            if not exited:
                if not isinstance(key, tuple):
                    continue
                timeout = client.upstream.resources.idle_timeout_seconds
                if timeout <= 0 or client.idle_seconds(now=current) < timeout:
                    continue
            remaining = client.stop()
            with self._stdio_upstreams_lock:
                if self._stdio_upstreams.get(key) is client:
                    self._stdio_upstreams.pop(key, None)
                    reaped.append((key, client, remaining))
                    event = "upstream.reaped_exited" if exited else "upstream.reaped_idle"
                    reaped_events.append((key, remaining, event))
        for key, remaining, event in reaped_events:
            self._write_log(
                event,
                upstream=_stdio_client_name(key),
                remaining_broker_processes=sorted(set(remaining)),
            )
        return reaped

    def _wake_server(self) -> None:
        if not self.socket_path.exists():
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.2)
                client.connect(str(self.socket_path))
                client.sendall(b'{"method":"broker/wake"}\n')
                client.recv(4096)
        except (OSError, TimeoutError):
            return

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            metadata = json.loads(self.lock_path.read_text(encoding="utf-8"))
            pid = int(metadata["pid"])
            if _process_exists(pid):
                raise BrokerDaemonError(f"broker daemon already running: pid {pid}")
            self.lock_path.unlink(missing_ok=True)
        self.lock_path.write_text(
            json.dumps({"owner": "mcp-broker", "pid": os.getpid()}, sort_keys=True),
            encoding="utf-8",
        )

    def _release_lock(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def _cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleanup_done:
                return
            self._shutdown_upstreams()
            server = self._server
            if server is not None:
                server.close()
                self._server = None
            self.socket_path.unlink(missing_ok=True)
            (self._paths.socket_owner_dir / f"{self.socket_path.name}.json").unlink(missing_ok=True)
            self._release_lock()
            if not self._stop_logged:
                self._write_log("daemon.stopped")
                self._stop_logged = True
            self._write_status_snapshot("stopped")
            self._cleanup_done = True

def main(argv: Sequence[str] | None = None) -> int:
    from mcp_broker.daemon_cli import main as daemon_cli_main

    return daemon_cli_main(argv, daemon_cls=BrokerDaemon, request_fn=_client_request)


def _broker_config_for_serve(config_path: str | Path | None) -> BrokerConfig | None:
    from mcp_broker.daemon_cli import _broker_config_for_serve as broker_config_for_serve

    return broker_config_for_serve(config_path)


def _broker_method_for_command(command: str) -> str:
    from mcp_broker.daemon_cli import _broker_method_for_command as broker_method_for_command

    return broker_method_for_command(command)


def _client_request(socket_path: Path, method: str) -> dict[str, object]:
    from mcp_broker.daemon_cli import _client_request as client_request

    return client_request(socket_path, method)


if __name__ == "__main__":
    raise SystemExit(main())
