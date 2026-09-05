"""Tool namespace mapping for configured upstream MCP servers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from mcp_broker.config import BrokerSettings, UpstreamConfig
from mcp_broker.profiles import BROKER_TOOL_NAME_STYLES, ToolExposureProfile


_CALL_TOOL_PROJECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional server-side projection applied to the upstream response before it "
        "reaches the client, so a verbose result is trimmed to only the fields you need "
        "and large arrays are capped. This cuts context tokens and latency. Omit it to "
        "receive the full, unmodified upstream response."
    ),
    "properties": {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Dotted field paths to keep, such as data.items.id. A path that reaches "
                "a list is applied to every element. Omit to keep all fields and only "
                "cap arrays with max_array_items."
            ),
        },
        "max_array_items": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Truncate every array in the projected response to at most this many "
                "items, so a long list returns only its first entries."
            ),
        },
    },
    "additionalProperties": False,
}


_COMPACT_BROKER_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "broker.search_tools": {
        "description": (
            "Search the profile-visible upstream MCP tool catalog without advertising every upstream "
            "tool to the client. Use this first when the task mentions a capability, service, keyword, "
            "or upstream prefix and you need candidate broker-qualified tool names before describing "
            "or calling one of them."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Search request for the broker catalog. The broker inspects only upstreams exposed to "
                "the active profile, applies mutating-upstream policy, and returns matching tool "
                "metadata plus skipped upstreams when discovery fails."
            ),
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Capability, upstream name, tool name fragment, or task keyword to match "
                        "against broker-qualified tool names, upstream names, descriptions, and schemas."
                    ),
                    "minLength": 1,
                    "examples": ["github issue", "file read", "browser screenshot"],
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of matching tools to return. Keep this small for exploration, "
                        "then refine the query or call broker_describe_tool for exact schema details."
                    ),
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "broker.describe_tool": {
        "description": (
            "Describe one profile-visible upstream MCP tool by broker-qualified name. Use this after "
            "broker_search_tools and before broker_call_tool so the client can inspect the exact "
            "description, input schema, upstream owner, transport, and mutating metadata instead of "
            "guessing arguments."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Describe request for one catalog entry. The name must be the full broker-qualified "
                "tool name returned by broker_search_tools or another catalog response, including the "
                "upstream prefix and namespace separator."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Full broker-qualified tool name to inspect, such as an upstream prefix joined "
                        "to the upstream tool name by the configured namespace separator."
                    ),
                    "minLength": 1,
                    "examples": ["github.get_me", "filesystem.read_file"],
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "broker.call_tool": {
        "description": (
            "Call one profile-visible upstream MCP tool through the broker using its broker-qualified "
            "name and exact argument object. The broker's mutating-tool policy remains gated by the "
            "active profile's allowlist, so call broker_describe_tool first and pass only arguments "
            "accepted by the described upstream schema."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Invocation request for a broker-managed upstream tool. The broker resolves the "
                "namespaced name, enforces profile exposure and mutating-tool policy, serializes calls "
                "when configured, and returns the upstream tool result."
            ),
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Full broker-qualified tool name to invoke. Use a name returned by "
                        "broker_search_tools or broker_describe_tool, not an unqualified upstream name."
                    ),
                    "minLength": 1,
                    "examples": ["github.get_file_contents", "filesystem.read_file"],
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "Exact JSON object accepted by the described upstream tool schema. Use an empty "
                        "object only when broker_describe_tool shows that the upstream tool accepts no "
                        "parameters."
                    ),
                    "default": {},
                    "additionalProperties": True,
                },
                "projection": _CALL_TOOL_PROJECTION_SCHEMA,
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        },
    },
    "broker.status": {
        "description": (
            "Report broker and upstream status for the active profile without starting hidden or denied "
            "upstreams. Use this for health checks, support tickets, and client validation because it "
            "returns the profile name, broker socket path, aggregate status, upstream runtime state, "
            "auth state, transport, process id, restarts, and mutating exposure."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Status request for the active broker profile. No user arguments are accepted; clients "
                "may pass transport-control fields that the broker ignores, but status filters them "
                "before validating the request."
            ),
            "properties": {},
            "additionalProperties": False,
        },
    },
    "broker.close_session": {
        "description": (
            "Release the broker-owned per-session upstream processes for the current client session. "
            "Use this after shutting down a client session when its session-scoped MCP subprocesses "
            "must exit promptly. This never stops shared upstreams or another client's session."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                "Close request for the calling broker session. The broker takes the session identity "
                "from client transport metadata; user arguments are not accepted."
            ),
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def compact_broker_tool_definitions(
    *,
    broker_tool_name_style: str | None = None,
) -> list[dict[str, Any]]:
    if broker_tool_name_style is None:
        broker_tool_name_style = "dotted"
    if broker_tool_name_style not in BROKER_TOOL_NAME_STYLES:
        allowed = ", ".join(sorted(BROKER_TOOL_NAME_STYLES))
        raise ValueError(f"broker_tool_name_style must be one of: {allowed}")
    return [
        _broker_tool(
            _compact_broker_tool_name(canonical_name, broker_tool_name_style),
            definition["description"],
            definition["inputSchema"],
        )
        for canonical_name, definition in _COMPACT_BROKER_TOOL_DEFINITIONS.items()
    ]


@dataclass(frozen=True)
class ToolRoute:
    upstream_name: str
    upstream_tool_name: str


class ToolNamespaceRouter:
    def __init__(
        self,
        *,
        broker: BrokerSettings,
        upstreams: dict[str, UpstreamConfig],
        profile: ToolExposureProfile | None = None,
    ) -> None:
        if not broker.tool_namespace_separator:
            raise ValueError("broker.tool_namespace_separator cannot be empty")
        self._separator = broker.tool_namespace_separator
        self._profile = profile
        self._upstreams = dict(upstreams)
        self._prefix_to_upstream = self._build_prefix_index(upstreams, profile)

    @staticmethod
    def _build_prefix_index(
        upstreams: dict[str, UpstreamConfig],
        profile: ToolExposureProfile | None,
    ) -> dict[str, str]:
        index: dict[str, str] = {}
        for upstream_name, upstream in upstreams.items():
            if not upstream.enabled or upstream.mode == "disabled":
                continue
            if profile is not None and profile.name not in upstream.profiles:
                continue
            prefix = upstream.tool_prefix or upstream.name
            if prefix in index:
                raise ValueError(f"duplicate tool prefix: {prefix}")
            index[prefix] = upstream_name
        return index

    def advertise_tools(
        self,
        upstream_name: str,
        upstream_tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        upstream = self._enabled_upstream(upstream_name)
        self._enforce_mutating_allowlist(upstream)
        prefix = upstream.tool_prefix or upstream.name
        advertised = []
        for tool in upstream_tools:
            tool_name = str(tool.get("name", ""))
            if not tool_name:
                raise ValueError(f"upstream tool missing name: {upstream_name}")
            namespaced = deepcopy(tool)
            namespaced["name"] = f"{prefix}{self._separator}{tool_name}"
            advertised.append(namespaced)
        return advertised

    def advertise_all_tools(
        self,
        upstream_tools: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        advertised = []
        names = set()
        for upstream_name, tools in upstream_tools.items():
            if self._upstream_allowed(upstream_name):
                for tool in self.advertise_tools(upstream_name, tools):
                    tool_name = str(tool["name"])
                    if tool_name in names:
                        raise ValueError(f"duplicate advertised tool: {tool_name}")
                    names.add(tool_name)
                    advertised.append(tool)
        self._enforce_profile_budget(len(advertised))
        return advertised

    def compact_broker_tools(self) -> list[dict[str, Any]]:
        profile = self._profile
        if profile is None or not profile.compact_tools_enabled:
            return []
        return compact_broker_tool_definitions(
            broker_tool_name_style=profile.broker_tool_name_style
        )

    def resolve_tool_name(self, advertised_name: str) -> ToolRoute:
        if self._separator not in advertised_name:
            raise ValueError(f"missing namespace separator: {advertised_name}")
        prefix, upstream_tool_name = advertised_name.split(self._separator, 1)
        if not upstream_tool_name:
            raise ValueError(f"missing upstream tool name: {advertised_name}")
        upstream_name = self._prefix_to_upstream.get(prefix)
        if upstream_name is None:
            raise ValueError(f"unknown tool prefix: {prefix}")
        return ToolRoute(
            upstream_name=upstream_name,
            upstream_tool_name=upstream_tool_name,
        )

    def _enabled_upstream(self, upstream_name: str) -> UpstreamConfig:
        upstream = self._upstreams.get(upstream_name)
        if upstream is None:
            raise ValueError(f"unknown upstream: {upstream_name}")
        if not upstream.enabled or upstream.mode == "disabled":
            raise ValueError(f"upstream disabled: {upstream_name}")
        if not self._upstream_allowed(upstream_name):
            raise ValueError(f"upstream not exposed to profile: {upstream_name}")
        return upstream

    def _upstream_allowed(self, upstream_name: str) -> bool:
        upstream = self._upstreams.get(upstream_name)
        if upstream is None or not upstream.enabled or upstream.mode == "disabled":
            return False
        if self._profile is None:
            return True
        return self._profile.name in upstream.profiles

    def _enforce_profile_budget(self, tool_count: int) -> None:
        if self._profile is None:
            return
        if tool_count > self._profile.max_tools:
            raise ValueError(
                f"profile {self._profile.name} exceeds tool budget: "
                f"{tool_count} > {self._profile.max_tools}"
            )

    def _enforce_mutating_allowlist(self, upstream: UpstreamConfig) -> None:
        if not upstream.mutating or self._profile is None:
            return
        if not self._profile.allows_mutating_upstream(upstream.name):
            raise ValueError(f"mutating upstream not allowed for profile: {upstream.name}")


def _broker_tool(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": deepcopy(input_schema),
    }


def _compact_broker_tool_name(canonical_name: str, broker_tool_name_style: str) -> str:
    if broker_tool_name_style == "snake":
        return canonical_name.replace(".", "_")
    return canonical_name
