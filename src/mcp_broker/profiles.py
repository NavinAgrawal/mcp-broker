"""Client profile definitions for broker tool exposure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


BROKER_TOOL_NAME_STYLES = frozenset({"dotted", "snake"})
_BROKER_TOOL_NAMES = {
    "broker.search_tools",
    "broker.describe_tool",
    "broker.call_tool",
    "broker.status",
    "broker.close_session",
}
_BROKER_TOOL_SNAKE_ALIASES = {
    name.replace(".", "_"): name
    for name in _BROKER_TOOL_NAMES
}


@dataclass(frozen=True)
class ClientRootMatch:
    """Route a session to a profile when its project root lives under a fixed parent.

    The match is a name-prefix on the immediate child of ``parent``: a session whose
    resolved ``client_cwd`` is the directory ``<parent>/<name_prefix>*`` (or anything
    under it) matches. Anchoring on a fixed ``parent`` prevents a stray cwd such as
    ``/tmp/genai-quiz-pro`` from matching while still honoring the prefix wildcard.
    """

    parent: Path
    name_prefix: str

    def __post_init__(self) -> None:
        if not self.name_prefix:
            raise ValueError("client_root_match.name_prefix cannot be empty")
        if not self.parent.is_absolute():
            raise ValueError("client_root_match.parent must be an absolute path")

    def matches(self, client_cwd: str | None) -> bool:
        if not client_cwd:
            return False
        try:
            resolved = Path(client_cwd).resolve()
        except (OSError, ValueError, RuntimeError):
            return False
        parent = self.parent.resolve()
        for candidate in (resolved, *resolved.parents):
            if candidate.parent == parent and candidate.name.startswith(self.name_prefix):
                return True
        return False

    @classmethod
    def from_mapping(cls, profile_name: str, data: Any) -> "ClientRootMatch":
        if not isinstance(data, dict):
            raise ValueError(f"profiles.{profile_name}.client_root_match must be a mapping")
        parent_raw = data.get("parent")
        name_prefix = data.get("name_prefix")
        if not isinstance(parent_raw, str) or not parent_raw:
            raise ValueError(f"profiles.{profile_name}.client_root_match.parent is required")
        if not isinstance(name_prefix, str) or not name_prefix:
            raise ValueError(f"profiles.{profile_name}.client_root_match.name_prefix is required")
        parent = Path(os.path.expanduser(os.path.expandvars(parent_raw)))
        return cls(parent=parent, name_prefix=name_prefix)


@dataclass(frozen=True)
class ToolExposureProfile:
    name: str
    max_tools: int
    compact_tools_enabled: bool = False
    broker_tool_name_style: str = "dotted"
    allow_mutating_upstreams: tuple[str, ...] = ()
    client_root_match: ClientRootMatch | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("profile name cannot be empty")
        if self.max_tools <= 0:
            raise ValueError("profile max_tools must be greater than 0")
        if not isinstance(self.broker_tool_name_style, str):
            raise ValueError("profile broker_tool_name_style must be a string")
        if self.broker_tool_name_style not in BROKER_TOOL_NAME_STYLES:
            allowed = ", ".join(sorted(BROKER_TOOL_NAME_STYLES))
            raise ValueError(f"profile broker_tool_name_style must be one of: {allowed}")
        for upstream_name in self.allow_mutating_upstreams:
            if not upstream_name:
                raise ValueError("profile allow_mutating_upstreams cannot include empty values")

    @classmethod
    def from_mapping(cls, name: str, data: dict[str, Any]) -> "ToolExposureProfile":
        if "max_tools" not in data:
            raise ValueError(f"profiles.{name}.max_tools is required")
        allow_mutating_upstreams = _parse_allow_mutating_upstreams(name, data)
        client_root_match = None
        if data.get("client_root_match") is not None:
            client_root_match = ClientRootMatch.from_mapping(name, data["client_root_match"])
        return cls(
            name=name,
            max_tools=int(data["max_tools"]),
            compact_tools_enabled=bool(data.get("compact_tools_enabled", False)),
            broker_tool_name_style=data.get("broker_tool_name_style", "dotted"),
            allow_mutating_upstreams=allow_mutating_upstreams,
            client_root_match=client_root_match,
        )

    def allows_mutating_upstream(self, upstream_name: str) -> bool:
        return upstream_name in self.allow_mutating_upstreams

    def exposed_broker_tool_name(self, canonical_name: str) -> str:
        if self.broker_tool_name_style == "snake":
            return canonical_name.replace(".", "_")
        return canonical_name

    def canonical_broker_tool_name(self, name: str) -> str:
        if name in _BROKER_TOOL_NAMES:
            return name
        if self.broker_tool_name_style == "snake":
            return _BROKER_TOOL_SNAKE_ALIASES.get(name, name)
        return name


def select_profile_for_cwd(
    profiles: Mapping[str, ToolExposureProfile],
    requested: ToolExposureProfile | None,
    client_cwd: str | None,
) -> ToolExposureProfile | None:
    """Return the routed profile when the session cwd matches one, else the requested profile.

    A profile carrying a ``client_root_match`` claims any session whose ``client_cwd`` is
    under its configured root, overriding the requested profile. With no match (or no cwd)
    the requested profile is returned unchanged. Profiles are scanned by name for stable
    ordering; first match wins.
    """
    if client_cwd:
        for name in sorted(profiles):
            profile = profiles[name]
            match = profile.client_root_match
            if match is not None and match.matches(client_cwd):
                return profile
    return requested


def _parse_allow_mutating_upstreams(name: str, data: dict[str, Any]) -> tuple[str, ...]:
    raw = data.get("allow_mutating_upstreams", [])
    if not isinstance(raw, list):
        raise ValueError(f"profiles.{name}.allow_mutating_upstreams must be a list")
    parsed: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value:
            raise ValueError(f"profiles.{name}.allow_mutating_upstreams must contain upstream names")
        parsed.append(value)
    return tuple(parsed)
