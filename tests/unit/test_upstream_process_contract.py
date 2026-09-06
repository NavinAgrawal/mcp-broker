import os
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from mcp_broker import upstream_process
from mcp_broker.config import UpstreamConfig
from mcp_broker.upstream_process import (
    _drain_pipe,
    _process_group_members,
    _start_drainer,
    _validate_registry_upstream_mode,
)


pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]


def test_registry_mode_guard_rejects_per_call_with_exact_error() -> None:
    upstream = UpstreamConfig(name="single-use", command="worker", mode="per_call")

    with pytest.raises(
        ValueError,
        match="^per_call upstreams require broker call lifecycle$",
    ):
        _validate_registry_upstream_mode(upstream)


def test_process_output_drainer_flushes_log_before_pipe_closes(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    log_path = tmp_path / "stdout.log"
    read_pipe = os.fdopen(read_fd, "rb", buffering=0)
    thread = _start_drainer(read_pipe, log_path)

    try:
        os.write(write_fd, b"visible-before-close\n")
        deadline = time.monotonic() + 1
        while (
            "visible-before-close" not in _read_if_exists(log_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        assert "visible-before-close" in log_path.read_text(encoding="utf-8")
    finally:
        os.close(write_fd)
        thread.join(timeout=1)


def test_process_group_members_runs_ps_with_fixed_capture_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append((command, kwargs))
        return CompletedProcess(command, 0, stdout=" 12\n\n 34 \n", stderr="")

    monkeypatch.setattr(upstream_process.subprocess, "run", fake_run)

    assert _process_group_members(9876) == (12, 34)
    assert calls == [
        (
            ["ps", "-o", "pid=", "-g", "9876"],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


def test_process_output_drainer_uses_daemon_thread(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    read_pipe = os.fdopen(read_fd, "rb", buffering=0)
    thread = _start_drainer(read_pipe, tmp_path / "stdout.log")

    try:
        assert thread.daemon is True
    finally:
        os.close(write_fd)
        thread.join(timeout=1)


def test_process_output_drainer_creates_nested_log_path(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "logs" / "stdout.log"

    _drain_pipe(_BytesPipe([b"after\n", b"done\n"]), log_path)

    assert log_path.read_bytes() == b"after\ndone\n"


def test_process_output_drainer_appends_existing_log(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "stdout.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"before\n")

    _drain_pipe(_BytesPipe([b"after\n", b"done\n"]), log_path)

    assert log_path.read_bytes() == b"before\nafter\ndone\n"


def _read_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


class _BytesPipe:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._eof_reads = 0
        self.closed = False

    def readline(self) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        self._eof_reads += 1
        if self._eof_reads > 1:
            raise AssertionError("drainer read after EOF")
        return b""

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_BytesPipe":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
