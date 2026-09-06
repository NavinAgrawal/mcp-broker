import os
import signal
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


def test_shared_upstream_reuses_one_process_for_two_sessions(tmp_path: Path) -> None:
    from mcp_broker.upstream_process import UpstreamProcessRegistry

    upstream = _upstream(tmp_path, name="read-store", mode="shared")
    registry = UpstreamProcessRegistry(runtime_state_dir=tmp_path / "runtime" / "state")

    first = registry.get_or_start(upstream, session_id="llm-session-a")
    second = registry.get_or_start(upstream, session_id="llm-session-b")
    first_pid = first.pid

    try:
        assert first is second
        assert first_pid is not None
        assert second.pid == first_pid
        assert os.getpgid(first_pid) == os.getpgid(second.pid)
    finally:
        registry.stop_all()

    assert first_pid is not None
    assert _process_exists(first_pid) is False


def test_per_session_upstreams_use_separate_process_groups(tmp_path: Path) -> None:
    from mcp_broker.upstream_process import UpstreamProcessRegistry

    upstream = _upstream(tmp_path, name="notes-writer", mode="per_session")
    registry = UpstreamProcessRegistry(runtime_state_dir=tmp_path / "runtime" / "state")

    first = registry.get_or_start(upstream, session_id="llm-session-a")
    same_session = registry.get_or_start(upstream, session_id="llm-session-a")
    second = registry.get_or_start(upstream, session_id="llm-session-b")
    first_pid = first.pid
    second_pid = second.pid

    try:
        assert same_session is first
        assert first is not second
        assert first_pid is not None
        assert second_pid is not None
        assert first_pid != second_pid
        assert os.getpgid(first_pid) != os.getpgid(second_pid)
    finally:
        registry.stop_all()

    assert first_pid is not None
    assert second_pid is not None
    assert _process_exists(first_pid) is False
    assert _process_exists(second_pid) is False


def test_per_session_upstream_requires_session_id(tmp_path: Path) -> None:
    from mcp_broker.upstream_process import UpstreamProcessRegistry

    upstream = _upstream(tmp_path, name="session-bound", mode="per_session")
    registry = UpstreamProcessRegistry(runtime_state_dir=tmp_path / "runtime" / "state")

    with pytest.raises(ValueError, match="session_id is required for per_session upstream"):
        registry.get_or_start(upstream, session_id=None)


def test_legacy_process_registry_rejects_per_call_mode(tmp_path: Path) -> None:
    from mcp_broker.upstream_process import UpstreamProcessRegistry

    upstream = _upstream(tmp_path, name="single-use", mode="per_call")
    registry = UpstreamProcessRegistry(runtime_state_dir=tmp_path / "runtime" / "state")

    with pytest.raises(ValueError, match="per_call upstreams require broker call lifecycle"):
        registry.get_or_start(upstream, session_id=None)


def _upstream(tmp_path: Path, *, name: str, mode: str):
    from mcp_broker.config import UpstreamConfig

    worker = tmp_path / f"{name}.py"
    worker.write_text(
        """
import signal
import sys
import time

signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    return UpstreamConfig(
        name=name,
        command=sys.executable,
        args=[str(worker)],
        mode=mode,
        enabled=True,
        state_dir=f"upstreams/{name}",
        tool_prefix=name,
    )


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
