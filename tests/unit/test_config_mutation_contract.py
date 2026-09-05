from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def _load_config(tmp_path: Path, body: str):
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(body.strip(), encoding="utf-8")
    return BrokerConfig.from_file(config_file)


def test_environment_expansion_is_bounded_to_five_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for source, target in zip("ABCDEF", "BCDEFG", strict=True):
        monkeypatch.setenv(source, f"${target}")
    monkeypatch.setenv("G", "expanded-too-far")

    config = _load_config(
        tmp_path,
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  bounded:
    command: $A
""",
    )

    assert config.upstreams["bounded"].command == "$F"


def test_distinct_tool_prefixes_can_share_a_profile(tmp_path: Path) -> None:
    config = _load_config(
        tmp_path,
        f"""
runtime:
  root: {tmp_path}/runtime
profiles:
  codex:
    max_tools: 10
upstreams:
  reader:
    command: reader
    profiles: [codex]
    tool_prefix: read
  writer:
    command: writer
    profiles: [codex]
    tool_prefix: write
""",
    )

    assert sorted(config.upstreams) == ["reader", "writer"]


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ("env:\n      1: HOST_TOKEN", "upstreams.reader.env keys"),
        ("env:\n      TOKEN: 1", "upstreams.reader.env.TOKEN"),
        ("env_files:\n      1: /tmp/token", "upstreams.reader.env_files keys"),
    ],
)
def test_upstream_environment_rejects_non_string_names(
    tmp_path: Path,
    environment: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _load_config(
            tmp_path,
            f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    {environment}
""",
        )


def test_upstream_environment_preserves_host_variable_mapping(tmp_path: Path) -> None:
    config = _load_config(
        tmp_path,
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    env:
      TOKEN: HOST_TOKEN
""",
    )

    assert config.upstreams["reader"].env == {"TOKEN": "HOST_TOKEN"}


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ("health: []", "upstreams.reader.health must be a mapping"),
        ("tool_timeouts: []", "upstreams.reader.tool_timeouts must be a mapping"),
        ("resources: []", "upstreams.reader.resources must be a mapping"),
    ],
)
def test_upstream_policy_errors_name_the_exact_policy_path(
    tmp_path: Path,
    policy: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _load_config(
            tmp_path,
            f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    {policy}
""",
        )


def test_upstream_resource_policy_preserves_custom_limits(tmp_path: Path) -> None:
    config = _load_config(
        tmp_path,
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    resources:
      idle_timeout_seconds: 45
      cpu_watchdog_percent: 55
      cpu_watchdog_seconds: 6
      memory_ceiling_mb: 128
""",
    )

    resources = config.upstreams["reader"].resources
    assert (
        resources.idle_timeout_seconds,
        resources.cpu_watchdog_percent,
        resources.cpu_watchdog_seconds,
        resources.memory_ceiling_mb,
    ) == (45, 55, 6, 128)


@pytest.mark.parametrize(
    ("tool_timeouts", "message"),
    [
        ("1: 30", "upstreams.reader.tool_timeouts keys"),
        ("read: 0", "upstreams.reader.tool_timeouts.read must be greater than 0"),
    ],
)
def test_tool_timeouts_reject_invalid_names_and_zero(
    tmp_path: Path,
    tool_timeouts: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _load_config(
            tmp_path,
            f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    tool_timeouts:
      {tool_timeouts}
""",
        )


def test_tool_timeout_accepts_one_second_and_preserves_value(tmp_path: Path) -> None:
    config = _load_config(
        tmp_path,
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  reader:
    command: reader
    tool_timeouts:
      read: 1
""",
    )

    assert config.upstreams["reader"].tool_timeouts == {"read": 1}
