"""Local upstream MCP process lifecycle supervision."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import BinaryIO, cast

from mcp_broker.config import UpstreamConfig
from mcp_broker.upstream_state import UpstreamState
from mcp_broker.watchdog import (
    sample_process_group_cpu_percent,
    sample_process_group_memory_mb,
)


STOP_TIMEOUT_SECONDS = 5.0
KILL_WAIT_SECONDS = 0.25
PROCESS_GROUP_VERIFY_SECONDS = 0.5
UpstreamEventLogger = Callable[[str, str, dict[str, object]], None]


@dataclass
class UpstreamProcessSupervisor:
    upstream: UpstreamConfig
    runtime_state_dir: Path
    event_logger: UpstreamEventLogger | None = None
    restart_count: int = 0
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _state: UpstreamState = field(default=UpstreamState.CONFIGURED, init=False, repr=False)
    _state_history: list[UpstreamState] = field(default_factory=list, init=False, repr=False)
    _stdout_drainer: threading.Thread | None = field(default=None, init=False, repr=False)
    _stderr_drainer: threading.Thread | None = field(default=None, init=False, repr=False)
    _last_activity_monotonic: float = field(default=0.0, init=False, repr=False)
    _last_restart_monotonic: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        initial_state = (
            UpstreamState.DISABLED
            if not self.upstream.enabled or self.upstream.mode == "disabled"
            else UpstreamState.CONFIGURED
        )
        self._transition(initial_state)
        if initial_state == UpstreamState.DISABLED:
            self._emit_event("upstream.disabled", state=initial_state.value)

    @property
    def state_dir(self) -> Path:
        configured = self.upstream.state_dir or f"upstreams/{self.upstream.name}"
        path = Path(configured)
        if path.is_absolute():
            return path
        return self.runtime_state_dir / path

    @property
    def state_snapshot_path(self) -> Path:
        return self.state_dir / "supervisor-state.json"

    @property
    def state(self) -> UpstreamState:
        self._refresh_process_state()
        return self._state

    @property
    def state_history(self) -> tuple[UpstreamState, ...]:
        return tuple(self._state_history)

    @property
    def pid(self) -> int | None:
        if self._process is None or self._process.poll() is not None:
            return None
        return self._process.pid

    @property
    def status(self) -> str:
        if self._process is None:
            return self._state.value if self._state == UpstreamState.DISABLED else "stopped"
        if self._process.poll() is None:
            return "running"
        self._transition(UpstreamState.FAILED)
        return "exited"

    def start(
        self,
        *,
        ready_check: Callable[[], bool] | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not self.upstream.enabled or self.upstream.mode == "disabled":
            self._transition(UpstreamState.DISABLED)
            self._emit_event("upstream.disabled", state=UpstreamState.DISABLED.value)
            raise ValueError(f"upstream disabled: {self.upstream.name}")
        if self.status == "running":
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._transition(UpstreamState.STARTING)
        self._emit_event("upstream.start", state=UpstreamState.STARTING.value)
        env = os.environ | self.upstream.resolve_environment(os.environ) | {
            "MCP_BROKER_UPSTREAM_STATE_DIR": str(self.state_dir)
        }
        try:
            self._process = subprocess.Popen(
                [self.upstream.command, *self.upstream.args],
                cwd=self.state_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            self._transition(UpstreamState.FAILED)
            self._transition(UpstreamState.BACKOFF)
            self._emit_event("upstream.backoff", state=UpstreamState.BACKOFF.value)
            raise
        self._start_output_drainers()
        self.record_activity()
        self._transition(UpstreamState.RUNNING)
        if ready_check is not None:
            try:
                self._wait_until_ready(ready_check, poll_interval_seconds)
            except (RuntimeError, TimeoutError):
                self.stop()
                self._transition(UpstreamState.FAILED)
                self._transition(UpstreamState.BACKOFF)
                self._emit_event("upstream.backoff", state=UpstreamState.BACKOFF.value)
                raise
        self._emit_event("upstream.ready", state=UpstreamState.RUNNING.value, pid=self.pid)

    def stop(self, timeout_seconds: float = STOP_TIMEOUT_SECONDS) -> None:
        if self._process is None:
            if self._state != UpstreamState.DISABLED:
                self._transition(UpstreamState.STOPPED)
                self._emit_event("upstream.stop", state=UpstreamState.STOPPED.value)
            return
        process = self._process
        pid = process.pid
        process_group_id = _process_group_id(pid)
        if process.poll() is None:
            self._transition(UpstreamState.DRAINING)
            self._transition(UpstreamState.STOPPING)
            _signal_process_group(pid, signal.SIGTERM)
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._emit_event("upstream.kill", signal="SIGKILL", reason="stop_timeout")
                _signal_process_group(pid, signal.SIGKILL)
                try:
                    process.wait(timeout=max(timeout_seconds, KILL_WAIT_SECONDS))
                except subprocess.TimeoutExpired:
                    self._transition(UpstreamState.FAILED)
                    raise
        _signal_process_group(pid, signal.SIGKILL)
        self._verify_process_group_stopped(process_group_id)
        self._process = None
        self._join_output_drainers()
        self._transition(UpstreamState.STOPPED)
        self._emit_event("upstream.stop", state=UpstreamState.STOPPED.value)

    def restart(self) -> None:
        self._emit_event("upstream.restart", restart_count=self.restart_count + 1)
        self.stop()
        self.restart_count += 1
        self.start()

    def restart_after_failure(self, *, monotonic_seconds: float | None = None) -> bool:
        now = monotonic_seconds if monotonic_seconds is not None else time.monotonic()
        if self.restart_count >= self.upstream.restart.max_attempts:
            self._transition(UpstreamState.FAILED)
            raise RuntimeError(f"restart circuit open: {self.upstream.name}")
        if (
            self._last_restart_monotonic is not None
            and now - self._last_restart_monotonic < self.upstream.restart.backoff_seconds
        ):
            self._transition(UpstreamState.BACKOFF)
            self._emit_event("upstream.backoff", state=UpstreamState.BACKOFF.value)
            return False
        if self.status == "running":
            self.stop()
        self.restart_count += 1
        self._last_restart_monotonic = now
        self._emit_event("upstream.restart", restart_count=self.restart_count)
        self.start()
        return True

    def record_activity(self, *, monotonic_seconds: float | None = None) -> None:
        self._last_activity_monotonic = (
            monotonic_seconds if monotonic_seconds is not None else time.monotonic()
        )

    def reap_if_idle(self, *, monotonic_seconds: float | None = None) -> bool:
        if self.status != "running":
            return False
        now = monotonic_seconds if monotonic_seconds is not None else time.monotonic()
        idle_seconds = now - self._last_activity_monotonic
        if idle_seconds < self.upstream.resources.idle_timeout_seconds:
            return False
        self.stop()
        return True

    def kill_if_spinning(
        self,
        *,
        cpu_percent: float,
        sample_window_seconds: float,
        monotonic_seconds: float | None = None,
    ) -> bool:
        if self.status != "running":
            return False
        now = monotonic_seconds if monotonic_seconds is not None else time.monotonic()
        progress_age = now - self._last_activity_monotonic
        if cpu_percent < self.upstream.resources.cpu_watchdog_percent:
            return False
        if sample_window_seconds < self.upstream.resources.cpu_watchdog_seconds:
            return False
        if progress_age < self.upstream.resources.cpu_watchdog_seconds:
            return False
        self.stop()
        self._transition(UpstreamState.FAILED)
        return True

    def kill_if_process_group_spinning(
        self,
        *,
        sample_window_seconds: float,
        monotonic_seconds: float | None = None,
        cpu_sampler: Callable[[int], float] | None = None,
    ) -> bool:
        pid = self.pid
        if pid is None:
            return False
        pgid = os.getpgid(pid)
        sampler = cpu_sampler or sample_process_group_cpu_percent
        return self.kill_if_spinning(
            cpu_percent=sampler(pgid),
            sample_window_seconds=sample_window_seconds,
            monotonic_seconds=monotonic_seconds,
        )

    def kill_if_memory_over_ceiling(self, *, memory_mb: float | None) -> bool:
        if self.status != "running":
            return False
        ceiling = self.upstream.resources.memory_ceiling_mb
        if ceiling is None or memory_mb is None:
            return False
        if memory_mb <= ceiling:
            return False
        self.stop()
        self._transition(UpstreamState.FAILED)
        return True

    def kill_if_process_group_over_memory(
        self,
        *,
        memory_sampler: Callable[[int], float | None] | None = None,
    ) -> bool:
        pid = self.pid
        if pid is None:
            return False
        pgid = os.getpgid(pid)
        sampler = memory_sampler or sample_process_group_memory_mb
        return self.kill_if_memory_over_ceiling(memory_mb=sampler(pgid))

    def _refresh_process_state(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is not None:
            self._transition(UpstreamState.FAILED)

    def _wait_until_ready(
        self,
        ready_check: Callable[[], bool],
        poll_interval_seconds: float,
    ) -> None:
        deadline = time.monotonic() + self.upstream.startup_timeout_seconds
        pause = threading.Event()
        while time.monotonic() < deadline:
            if ready_check():
                self.record_activity()
                return
            if self.status == "exited":
                raise RuntimeError(f"upstream exited during startup: {self.upstream.name}")
            pause.wait(timeout=poll_interval_seconds)
        raise TimeoutError(f"upstream startup timed out: {self.upstream.name}")

    def _start_output_drainers(self) -> None:
        process = self._process
        assert process is not None
        self._stdout_drainer = _start_drainer(
            cast(BinaryIO, process.stdout),
            self.state_dir / "stdout.log",
        )
        self._stderr_drainer = _start_drainer(
            cast(BinaryIO, process.stderr),
            self.state_dir / "stderr.log",
        )

    def _join_output_drainers(self) -> None:
        for drainer in (self._stdout_drainer, self._stderr_drainer):
            if drainer is not None:
                drainer.join(timeout=KILL_WAIT_SECONDS)
        self._stdout_drainer = None
        self._stderr_drainer = None

    def _transition(self, state: UpstreamState) -> None:
        if self._state_history and self._state_history[-1] == state:
            self._state = state
            self._write_state_snapshot()
            return
        self._state = state
        self._state_history.append(state)
        self._write_state_snapshot()

    def _write_state_snapshot(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_snapshot_path.write_text(
            json.dumps(
                {
                    "upstream": self.upstream.name,
                    "state": self._state.value,
                    "pid": self.pid,
                    "restart_count": self.restart_count,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _verify_process_group_stopped(
        self,
        process_group_id: int | None,
        *,
        verify_seconds: float = PROCESS_GROUP_VERIFY_SECONDS,
    ) -> None:
        if process_group_id is None:
            return
        deadline = time.monotonic() + verify_seconds
        pause = threading.Event()
        while time.monotonic() < deadline:
            if not _process_group_members(process_group_id):
                return
            pause.wait(timeout=0.01)
        members = _process_group_members(process_group_id)
        if members:
            self._transition(UpstreamState.FAILED)
            joined = ", ".join(str(pid) for pid in members)
            raise RuntimeError(
                f"process group still has members after stop: {process_group_id}: {joined}"
            )

    def _emit_event(self, event: str, **fields: object) -> None:
        if self.event_logger is None:
            return
        self.event_logger(event, self.upstream.name, fields)


def _validate_registry_upstream_mode(upstream: UpstreamConfig) -> None:
    if upstream.mode == "per_call":
        raise ValueError("per_call upstreams require broker call lifecycle")


@dataclass
class UpstreamProcessRegistry:
    runtime_state_dir: Path
    _supervisors: dict[tuple[str, str | None], UpstreamProcessSupervisor] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def get_or_start(
        self,
        upstream: UpstreamConfig,
        *,
        session_id: str | None,
    ) -> UpstreamProcessSupervisor:
        _validate_registry_upstream_mode(upstream)
        key = self._registry_key(upstream, session_id=session_id)
        supervisor = self._supervisors.get(key)
        if supervisor is None:
            supervisor = UpstreamProcessSupervisor(
                upstream=upstream,
                runtime_state_dir=self.runtime_state_dir,
            )
            self._supervisors[key] = supervisor
        supervisor.start()
        return supervisor

    def stop_all(self) -> None:
        for supervisor in self._supervisors.values():
            supervisor.stop()

    @staticmethod
    def _registry_key(
        upstream: UpstreamConfig,
        *,
        session_id: str | None,
    ) -> tuple[str, str | None]:
        if upstream.mode == "per_session":
            if not session_id:
                raise ValueError("session_id is required for per_session upstream")
            return (upstream.name, session_id)
        return (upstream.name, None)


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (PermissionError, ProcessLookupError):
        return


def _process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _process_group_members(process_group_id: int) -> tuple[int, ...]:
    result = subprocess.run(
        ["ps", "-o", "pid=", "-g", str(process_group_id)],
        check=False,
        capture_output=True,
        text=True,
    )
    return _parse_process_group_members(result.stdout)


def _parse_process_group_members(stdout: str) -> tuple[int, ...]:
    members: list[int] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            members.append(int(stripped))
    return tuple(members)


def _start_drainer(
    pipe: BinaryIO,
    log_path: Path,
) -> threading.Thread:
    thread = threading.Thread(
        target=_drain_pipe,
        args=(pipe, log_path),
        daemon=True,
    )
    thread.start()
    return thread


def _drain_pipe(pipe: BinaryIO, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with pipe, log_path.open("ab") as log_file:
        chunk = pipe.readline()
        while chunk != b"":
            log_file.write(chunk)
            log_file.flush()
            chunk = pipe.readline()
