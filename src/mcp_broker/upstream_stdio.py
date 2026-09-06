"""JSON-RPC over stdio for broker-owned upstream subprocesses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time
from typing import IO, Any, Callable, cast

from mcp_broker import __version__
from mcp_broker.config import UpstreamConfig
from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
from mcp_broker.runtime_reaper import RuntimePaths, write_process_metadata
from mcp_broker.upstream_process import (
    KILL_WAIT_SECONDS,
    PROCESS_GROUP_VERIFY_SECONDS,
    STOP_TIMEOUT_SECONDS,
)
from mcp_broker.watchdog import sample_process_group_cpu_percent, sample_process_group_memory_mb


class StdioUpstreamError(Exception):
    """Raised when a stdio upstream cannot complete a JSON-RPC call."""


class StdioUpstreamTimeout(StdioUpstreamError):
    """Raised when a stdio upstream does not respond before the deadline."""


UpstreamEventLogger = Callable[[str, str, dict[str, object]], None]


class StdioUpstreamProcess:
    def __init__(
        self,
        upstream: UpstreamConfig,
        *,
        runtime_state_dir: Path,
        session_context: dict[str, str] | None = None,
        event_logger: UpstreamEventLogger | None = None,
        runtime_paths: RuntimePaths | None = None,
        process_metadata_name: str | None = None,
    ) -> None:
        self.upstream = upstream
        self.runtime_state_dir = runtime_state_dir
        self.session_context = {} if session_context is None else session_context
        self._event_logger = event_logger
        self._runtime_paths = runtime_paths
        self._process_metadata_name = process_metadata_name or upstream.name
        self._process: subprocess.Popen[bytes] | None = None
        self._process_metadata_path: Path | None = None
        self._stderr_drainer: threading.Thread | None = None
        self._lock = threading.Lock()
        # Dedicated tiny lock for the activity timestamp. NEVER the call lock
        # (self._lock) - that one is held for a full upstream round-trip, and the
        # idle janitor must read the timestamp without stalling behind a slow call.
        self._activity_lock = threading.Lock()
        self._last_activity_monotonic = time.monotonic()
        self._next_id = 0
        self._restart_count = 0
        self._last_error: str | None = None
        self._initialized = False
        self._stdout_buffer = b""

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            return

    @property
    def state_dir(self) -> Path:
        configured = self.upstream.state_dir or f"upstreams/{self.upstream.name}"
        path = Path(configured)
        if path.is_absolute():
            return path
        return self.runtime_state_dir / path

    def record_activity(self, *, monotonic_seconds: float | None = None) -> None:
        """Stamp client-driven activity (call/list). Read by the idle janitor."""
        stamp = time.monotonic() if monotonic_seconds is None else monotonic_seconds
        with self._activity_lock:
            self._last_activity_monotonic = stamp

    def idle_seconds(self, *, now: float | None = None) -> float:
        """Seconds since the last client-driven activity."""
        current = time.monotonic() if now is None else now
        with self._activity_lock:
            return current - self._last_activity_monotonic

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        with self._lock:
            self.record_activity()
            self._emit_event(
                "upstream.call",
                method="tools/call",
                tool_name=tool_name,
                timeout_seconds=timeout_seconds,
            )
            try:
                result = self._call_tool_locked(
                    tool_name,
                    arguments,
                    timeout_seconds=timeout_seconds,
                )
            except StdioUpstreamTimeout:
                self._last_error = f"upstream timed out: {self.upstream.name}"
                self._emit_event(
                    "upstream.timeout",
                    method="tools/call",
                    tool_name=tool_name,
                    timeout_seconds=timeout_seconds,
                )
                self._reset_after_timeout_locked()
                raise
            except Exception as exc:
                self._last_error = str(exc)
                raise
            self._last_error = None
            return result

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, Any]]:
        with self._lock:
            self.record_activity()
            self._emit_event(
                "upstream.call",
                method="tools/list",
                timeout_seconds=timeout_seconds,
            )
            try:
                result = self._jsonrpc_request_locked(
                    "tools/list",
                    None,
                    timeout_seconds=timeout_seconds,
                )
                tools = result.get("tools")
                if not isinstance(tools, list) or not all(
                    isinstance(tool, dict) for tool in tools
                ):
                    raise StdioUpstreamError(
                        f"upstream tools/list response invalid: {self.upstream.name}"
                    )
            except StdioUpstreamTimeout:
                self._last_error = f"upstream timed out: {self.upstream.name}"
                self._emit_event(
                    "upstream.timeout",
                    method="tools/list",
                    timeout_seconds=timeout_seconds,
                )
                self._reset_after_timeout_locked()
                raise
            except Exception as exc:
                self._last_error = str(exc)
                raise
            self._last_error = None
            return tools

    def health_snapshot(self) -> dict[str, object]:
        pid = self.pid
        if pid is None:
            return self._inactive_health(self.status)
        try:
            process_group_id = os.getpgid(pid)
        except ProcessLookupError:
            return self._inactive_health("exited")
        return {
            "state": "running",
            "pid": pid,
            "cpu_percent": sample_process_group_cpu_percent(process_group_id),
            "memory_mb": sample_process_group_memory_mb(process_group_id),
            "restarts": self._restart_count,
            "last_error": self._last_error,
        }

    @property
    def pid(self) -> int | None:
        if self._process is None or self._process.poll() is not None:
            return None
        return self._process.pid

    @property
    def status(self) -> str:
        if self._process is None:
            return "configured"
        if self._process.poll() is None:
            return "running"
        return "exited"

    def ensure_running(self) -> None:
        with self._lock:
            try:
                self._start()
            except StdioUpstreamError as exc:
                self._last_error = str(exc)
                raise
            except Exception as exc:
                self._last_error = str(exc)
                raise StdioUpstreamError(
                    f"upstream failed to restart: {self.upstream.name}: {exc}"
                ) from exc
            self._last_error = None

    def stop(self) -> tuple[int, ...]:
        process = self._process
        if process is None:
            return ()
        process_group_id = _process_group_id(process.pid)
        if process.poll() is None:
            _signal_process_group(process_group_id, signal.SIGTERM)
            try:
                process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._emit_event("upstream.kill", signal="SIGKILL", reason="stop_timeout")
                _signal_process_group(process_group_id, signal.SIGKILL)
                try:
                    process.wait(timeout=max(STOP_TIMEOUT_SECONDS, KILL_WAIT_SECONDS))
                except subprocess.TimeoutExpired:
                    self._last_error = f"upstream did not exit after SIGKILL: {self.upstream.name}"
        self._emit_event("upstream.kill", signal="SIGKILL", reason="final_cleanup")
        _signal_process_group(process_group_id, signal.SIGKILL)
        remaining_processes = _wait_for_process_group_stop(process_group_id)
        if process.poll() is None:
            try:
                process.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                self._last_error = f"upstream did not exit after final SIGKILL: {self.upstream.name}"
                try:
                    process.kill()
                    process.wait(timeout=KILL_WAIT_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    self._last_error = (
                        f"upstream did not exit after direct SIGKILL: {self.upstream.name}"
                    )
        _close_process_pipes(process, include_stderr=False)
        self._process = None
        if not remaining_processes:
            self._remove_process_metadata()
        if self._stderr_drainer is not None:
            self._stderr_drainer.join(timeout=KILL_WAIT_SECONDS)
            self._stderr_drainer = None
        _close_process_pipes(process, include_stderr=True)
        self._emit_event("upstream.stop", state="stopped")
        return remaining_processes

    def _reset_after_timeout_locked(self) -> None:
        last_error = self._last_error
        self.stop()
        self._initialized = False
        self._stdout_buffer = b""
        self._restart_count += 1
        self._last_error = last_error
        self._emit_event(
            "upstream.restart",
            restart_count=self._restart_count,
            reason="timeout",
        )

    def _inactive_health(self, state: str) -> dict[str, object]:
        return {
            "state": state,
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": self._restart_count,
            "last_error": self._last_error,
        }

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            _close_process_pipes(self._process, include_stderr=True)
            self._remove_process_metadata()
            if self._stderr_drainer is not None:
                self._stderr_drainer.join(timeout=KILL_WAIT_SECONDS)
                self._stderr_drainer = None
            self._restart_count += 1
            self._emit_event("upstream.restart", restart_count=self._restart_count)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        session_environment = self.upstream.resolve_session_environment(self.session_context)
        env = os.environ | self.upstream.resolve_environment(os.environ) | session_environment | {
            "MCP_BROKER_CLIENT_CWD": self.session_context.get("client_cwd", ""),
            "MCP_BROKER_UPSTREAM_STATE_DIR": str(self.state_dir)
        }
        cwd = self.upstream.working_dir or self.state_dir
        self._emit_event("upstream.start", state="starting")
        try:
            self._process = subprocess.Popen(
                [self.upstream.command, *self.upstream.args],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self._emit_event("upstream.backoff", state="backoff")
            raise StdioUpstreamError(f"upstream failed to start: {self.upstream.name}: {exc}") from exc
        self._write_process_metadata()
        self._initialized = False
        self._stdout_buffer = b""
        stderr = self._process.stderr
        if stderr is None:
            raise StdioUpstreamError(f"upstream stderr closed: {self.upstream.name}")
        self._stderr_drainer = _start_stderr_drainer(
            stderr,
            self.state_dir / "stderr.log",
        )
        self._emit_event("upstream.ready", state="running", pid=self.pid)

    def _write_process_metadata(self) -> None:
        if self._runtime_paths is None or self._process is None:
            return
        pid = self._process.pid
        self._process_metadata_path = write_process_metadata(
            self._runtime_paths,
            name=self._process_metadata_name,
            pid=pid,
            process_group_id=os.getpgid(pid),
            broker_pid=os.getpid(),
        )

    def _remove_process_metadata(self) -> None:
        metadata_path = self._process_metadata_path
        if metadata_path is None and self._runtime_paths is not None:
            metadata_path = (
                self._runtime_paths.upstream_pid_dir / f"{self._process_metadata_name}.json"
            )
        if metadata_path is not None:
            metadata_path.unlink(missing_ok=True)
        self._process_metadata_path = None

    def _call_tool_locked(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": tool_name, "arguments": arguments}
        request_meta = self.upstream.resolve_request_meta(os.environ)
        if request_meta:
            params["_meta"] = request_meta
        return self._jsonrpc_request_locked(
            "tools/call",
            params,
            timeout_seconds=timeout_seconds,
        )

    def _jsonrpc_request_locked(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self._start()
        process = self._process
        assert process is not None
        if not self._initialized and (
            method == "tools/list" or self.upstream.strict_initialization
        ):
            self._initialize_upstream(process, timeout_seconds=timeout_seconds)
        request_id, payload = self._jsonrpc_payload(method, params)
        response = self._roundtrip(
            process,
            payload,
            timeout_seconds=timeout_seconds,
            expected_id=request_id,
        )
        if _response_is_not_initialized(response) and not self._initialized:
            self._initialize_upstream(process, timeout_seconds=timeout_seconds)
            request_id, payload = self._jsonrpc_payload(method, params)
            response = self._roundtrip(
                process,
                payload,
                timeout_seconds=timeout_seconds,
                expected_id=request_id,
            )
        return self._result_from_response(response, request_id)

    def _jsonrpc_payload(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        return request_id, payload

    def _roundtrip(
        self,
        process: subprocess.Popen[bytes],
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
        expected_id: str | int | None = None,
    ) -> dict[str, Any]:
        self._write_request(process, payload)
        return self._read_response(
            process,
            timeout_seconds=timeout_seconds,
            expected_id=expected_id,
        )

    def _initialize_upstream(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: int,
    ) -> None:
        request_id, payload = self._jsonrpc_payload(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {},
                "clientInfo": {"name": "mcp-broker", "version": __version__},
            },
        )
        response = self._roundtrip(
            process,
            payload,
            timeout_seconds=timeout_seconds,
            expected_id=request_id,
        )
        self._result_from_response(response, request_id)
        self._write_request(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self._initialized = True

    def _result_from_response(
        self,
        response: dict[str, Any],
        request_id: str | int,
    ) -> dict[str, Any]:
        if response.get("id") != request_id:
            raise StdioUpstreamError(
                f"upstream response id mismatch: {self.upstream.name}: "
                f"expected {request_id}, received {_response_identity(response)}"
            )
        error = response.get("error")
        if isinstance(error, dict):
            raise StdioUpstreamError(
                f"upstream error from {self.upstream.name}: {_format_response_error(error)}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise StdioUpstreamError(f"upstream response missing result: {self.upstream.name}")
        return result

    def _write_request(self, process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
        stdin = process.stdin
        if stdin is None:
            raise StdioUpstreamError(f"upstream stdin closed: {self.upstream.name}")
        try:
            stdin.write(json.dumps(payload, sort_keys=True).encode() + b"\n")
            stdin.flush()
        except BrokenPipeError as exc:
            raise StdioUpstreamError(f"upstream stdin closed: {self.upstream.name}") from exc

    def _read_response(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_seconds: int,
        expected_id: str | int | None = None,
    ) -> dict[str, Any]:
        stdout = process.stdout
        if stdout is None:
            raise StdioUpstreamError(f"upstream stdout closed: {self.upstream.name}")
        deadline = time.monotonic() + timeout_seconds
        while True:
            raw = self._read_stdout_line(stdout, deadline=deadline, process=process)
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise StdioUpstreamError(
                    f"upstream response must be an object: {self.upstream.name}"
                )
            if expected_id is not None and _is_jsonrpc_notification(loaded):
                continue
            return loaded

    def _read_stdout_line(
        self,
        stdout: IO[bytes],
        *,
        deadline: float,
        process: subprocess.Popen[bytes] | None = None,
    ) -> bytes:
        while b"\n" not in self._stdout_buffer:
            remaining = max(0, deadline - time.monotonic())
            readable, _, _ = select.select([stdout], [], [], remaining)
            if not readable:
                if process is not None and process.poll() is not None:
                    raise StdioUpstreamError(
                        f"upstream exited without response: {self.upstream.name}"
                    )
                raise StdioUpstreamTimeout(f"upstream timed out: {self.upstream.name}")
            chunk = os.read(stdout.fileno(), 4096)
            if not chunk:
                raise StdioUpstreamError(f"upstream exited without response: {self.upstream.name}")
            self._stdout_buffer += chunk
        raw, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
        return raw

    def _emit_event(self, event: str, **fields: object) -> None:
        if self._event_logger is None:
            return
        self._event_logger(event, self.upstream.name, fields)


def _start_stderr_drainer(stream: IO[bytes], path: Path) -> threading.Thread:
    def drain() -> None:
        with path.open("ab") as handle:
            while chunk := _read_stderr_chunk(stream):
                handle.write(chunk)
                handle.flush()

    thread = threading.Thread(target=drain, name=f"mcp-broker-stderr-{path.parent.name}")
    thread.daemon = True
    thread.start()
    return thread


def _read_stderr_chunk(stream: IO[bytes] | None) -> bytes:
    if stream is None:
        return b""
    try:
        return stream.read(4096)
    except ValueError:
        return b""


def _signal_process_group(process_group_id: int | None, sig: signal.Signals) -> None:
    if process_group_id is None:
        return
    try:
        os.killpg(process_group_id, sig)
    except (PermissionError, ProcessLookupError):
        return


def _process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _wait_for_process_group_stop(process_group_id: int | None) -> tuple[int, ...]:
    if process_group_id is None:
        return ()
    deadline = time.monotonic() + PROCESS_GROUP_VERIFY_SECONDS
    pause = threading.Event()
    while time.monotonic() < deadline:
        members = _process_group_members(process_group_id)
        if not members:
            return ()
        pause.wait(timeout=0.01)
    return _process_group_members(process_group_id)


def _response_is_not_initialized(response: dict[str, Any]) -> bool:
    error = response.get("error")
    if not isinstance(error, dict):
        return False
    if (
        error.get("code") == -32602
        and error.get("message") == "Invalid request parameters"
        and error.get("data") == ""
    ):
        return True
    message = error.get("message")
    if error.get("code") == -32002:
        return True
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return "not initialized" in lowered or "before initialization was complete" in lowered


def _is_jsonrpc_notification(message: dict[str, Any]) -> bool:
    return "id" not in message and isinstance(message.get("method"), str)


def _response_identity(message: dict[str, Any]) -> str:
    parts = [f"id={message.get('id')!r}"]
    method = message.get("method")
    if isinstance(method, str):
        parts.append(f"method={method!r}")
    return ", ".join(parts)


def _format_response_error(error: dict[str, Any]) -> str:
    parts: list[str] = []
    code = error.get("code")
    if code is not None:
        parts.append(str(code))
    message = error.get("message")
    if message is not None:
        parts.append(str(message))
    data = error.get("data")
    if data is not None:
        parts.append(f"data={data!r}")
    if not parts:
        return repr(error)
    return " ".join(parts)


def _process_group_members(process_group_id: int) -> tuple[int, ...]:
    result = subprocess.run(
        ["ps", "-o", "pid=", "-g", str(process_group_id)],
        check=False,
        capture_output=True,
        text=True,
    )
    members: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            members.append(int(stripped))
    return tuple(members)


def _close_process_pipes(
    process: subprocess.Popen[bytes],
    *,
    include_stderr: bool,
) -> None:
    streams = [process.stdin, process.stdout]
    if include_stderr:
        streams.append(process.stderr)
    for stream in streams:
        if stream is not None:
            stream.close()
