"""Broker facade catalog behavior."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from typing import Any, Callable

from mcp_broker.broker import BrokerCore, BrokerToolError
from mcp_broker.config import BrokerConfig, UpstreamConfig
from mcp_broker.project_slug import inject_cwd_project
from mcp_broker.profiles import ToolExposureProfile


ToolLister = Callable[[str, int], list[dict[str, object]]]
ToolCaller = Callable[[str, str, dict[str, Any], int], dict[str, Any]]
StatusProvider = Callable[[set[str] | None], dict[str, dict[str, object]]]
SessionStopper = Callable[[str], dict[str, object]]
_CLIENT_CONTROL_ARGUMENTS = frozenset({"wait_for_previous"})


class BrokerCatalogFacade:
    def __init__(
        self,
        *,
        broker_config: BrokerConfig,
        profile: ToolExposureProfile | None,
        list_upstream: ToolLister,
        call_upstream: ToolCaller,
        call_locks: dict[str, threading.Lock],
        status_provider: StatusProvider | None = None,
        client_cwd: str | None = None,
        session_id: str | None = None,
        session_stopper: SessionStopper | None = None,
    ) -> None:
        self._broker_config = broker_config
        self._profile = profile
        self._list_upstream = list_upstream
        self._call_upstream = call_upstream
        self._call_locks = call_locks
        self._status_provider = status_provider or (lambda _visible_upstreams: {})
        self._client_cwd = client_cwd
        self._session_id = session_id
        self._session_stopper = session_stopper

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        canonical_name = self._canonical_broker_tool_name(name)
        if canonical_name == "broker.search_tools":
            return self._search_tools(arguments)
        if canonical_name == "broker.describe_tool":
            return self._describe_tool(arguments)
        if canonical_name == "broker.call_tool":
            return self._call_managed_tool(arguments)
        if canonical_name == "broker.status":
            return self._status(arguments)
        if canonical_name == "broker.close_session":
            return self._close_session(arguments)
        raise BrokerToolError(code="unknown_broker_tool", message=f"unknown broker tool: {name}")

    def _canonical_broker_tool_name(self, name: str) -> str:
        if self._profile is None:
            return name
        return self._profile.canonical_broker_tool_name(name)

    def _search_tools(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        limit = int(arguments.get("limit", 20))
        entries, skipped_upstreams = self._catalog_entries(query=query, tool_name="")
        # Every catalog entry carries a "name" (tool entries and unavailable stubs
        # both set it), so index it directly - a default here would be dead code.
        scored = [
            (catalog_entry_score(entry, query), str(entry["name"]), entry)
            for entry in entries
        ]
        matches = [
            slim_catalog_entry(entry)
            for score, _name, entry in sorted(scored, key=lambda item: (-item[0], item[1]))
            if score > 0
        ][:limit]
        result: dict[str, Any] = {"matches": matches}
        if skipped_upstreams:
            result["skipped_upstreams"] = skipped_upstreams
        return structured_tool_result(result)

    def _describe_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_name = arguments.get("name")
        if not isinstance(tool_name, str):
            raise ValueError("broker.describe_tool requires string name")
        entries, _skipped_upstreams = self._catalog_entries(query="", tool_name=tool_name)
        for entry in entries:
            if entry["name"] == tool_name:
                return structured_tool_result({"tool": entry})
        raise ValueError(f"broker tool not found: {tool_name}")

    def _call_managed_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_name = arguments.get("name")
        tool_arguments = arguments.get("arguments", {})
        if not isinstance(tool_name, str) or not isinstance(tool_arguments, dict):
            raise ValueError("broker.call_tool requires name and object arguments")
        projection = arguments.get("projection")
        if projection is not None and not isinstance(projection, dict):
            raise ValueError("broker.call_tool projection must be an object")
        tool_arguments = self._inject_cwd_project_arg(tool_name, tool_arguments)
        core = BrokerCore(
            settings=self._broker_config.broker,
            upstreams=self._broker_config.upstreams,
            profile=self._profile,
            call_locks=self._call_locks,
        )
        response = core.call_tool(tool_name, tool_arguments, self._call_upstream)
        if projection is None:
            return response
        return apply_projection(response, projection)

    def _inject_cwd_project_arg(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._client_cwd:
            return arguments
        separator = self._broker_config.broker.tool_namespace_separator or "."
        for upstream in self._broker_config.upstreams.values():
            if not upstream.inject_cwd_project:
                continue
            arguments = inject_cwd_project(
                name,
                arguments,
                self._client_cwd,
                tool_prefix=upstream.tool_prefix or upstream.name,
                separator=separator,
                exclude=upstream.inject_cwd_project_exclude,
            )
        return arguments

    def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        status_arguments = {
            name: value
            for name, value in arguments.items()
            if name not in _CLIENT_CONTROL_ARGUMENTS
        }
        if status_arguments:
            raise ValueError("broker.status does not accept arguments")
        exposed_upstreams = {
            upstream_name
            for upstream_name, upstream in self._broker_config.upstreams.items()
            if self._status_exposes_upstream(upstream_name, upstream)
        }
        health = self._status_provider(exposed_upstreams)
        upstreams = self._status_upstreams(health)
        return structured_tool_result(
            {
                "identity": self._broker_config.identity_status_payload(
                    active_profile=self._profile.name if self._profile is not None else None,
                ),
                "profile": self._profile.name if self._profile is not None else None,
                "socket_path": str(self._broker_config.runtime.socket_path),
                "status": _broker_status(upstreams),
                "upstreams": upstreams,
            }
        )

    def _close_session(self, arguments: dict[str, Any]) -> dict[str, Any]:
        close_arguments = {
            name: value
            for name, value in arguments.items()
            if name not in _CLIENT_CONTROL_ARGUMENTS
        }
        if close_arguments:
            raise ValueError("broker.close_session does not accept arguments")
        if self._session_id is None or self._session_stopper is None:
            raise ValueError("broker.close_session requires broker session metadata")
        return structured_tool_result(self._session_stopper(self._session_id))

    def _status_upstreams(self, health: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
        upstreams = {}
        for upstream_name, upstream in self._broker_config.upstreams.items():
            exposed = self._status_exposes_upstream(upstream_name, upstream)
            if not exposed and upstream.enabled and upstream.mode != "disabled":
                continue
            snapshot = health.get(upstream_name, {})
            upstreams[upstream_name] = {
                "enabled": upstream.enabled,
                "auth_repair_attempts": _snapshot_int(snapshot, "auth_repair_attempts"),
                "auth_repair_failures": _snapshot_int(snapshot, "auth_repair_failures"),
                "auth_repair_successes": _snapshot_int(snapshot, "auth_repair_successes"),
                "auth_probe": str(snapshot.get("auth_probe", "none")),
                "auth_state": _auth_state(snapshot),
                "exposed": exposed,
                "last_error": snapshot.get("last_error"),
                "mode": upstream.mode,
                "mutating": upstream.mutating,
                "pid": snapshot.get("pid"),
                "restarts": snapshot.get("restarts"),
                "session_count": _snapshot_int(snapshot, "session_count", "sessions"),
                "state": snapshot.get(
                    "state",
                    "configured" if upstream.enabled and upstream.mode != "disabled" else "disabled",
                ),
                "transport": upstream.transport,
            }
        return upstreams

    def _status_exposes_upstream(self, upstream_name: str, upstream: UpstreamConfig) -> bool:
        if not upstream.enabled or upstream.mode == "disabled":
            return False
        if not profile_allows_upstream(self._profile, upstream):
            return False
        return not (
            upstream.mutating
            and self._profile is not None
            and not self._profile.allows_mutating_upstream(upstream_name)
        )

    def _catalog_entries(
        self,
        query: str,
        tool_name: str,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if not isinstance(query, str) or not isinstance(tool_name, str):
            raise TypeError("query and tool_name must be strings")
        if query and tool_name:
            raise ValueError("use query or tool_name, not both")
        entries = []
        skipped_upstreams = {}
        for upstream_name, upstream in self._catalog_upstreams(
            query=query,
            tool_name=tool_name,
        ).items():
            try:
                tools = self._list_upstream(upstream_name, upstream.health.call_timeout_seconds)
            except Exception as exc:
                error = str(exc)
                skipped_upstreams[upstream_name] = error
                entries.append(catalog_unavailable_entry_for_upstream(upstream, error))
                continue
            entries.extend(
                catalog_entries_for_upstream(
                    upstream,
                    tools,
                    self._broker_config.broker.tool_namespace_separator,
                )
            )
        return entries, skipped_upstreams

    def _catalog_upstreams(
        self,
        query: str,
        tool_name: str,
    ) -> dict[str, UpstreamConfig]:
        upstreams = {}
        for upstream_name, upstream in self._broker_config.upstreams.items():
            if not upstream.enabled or upstream.mode == "disabled":
                continue
            if not profile_allows_upstream(self._profile, upstream):
                continue
            if (
                upstream.mutating
                and self._profile is not None
                and not self._profile.allows_mutating_upstream(upstream_name)
            ):
                continue
            upstreams[upstream_name] = upstream
        if tool_name:
            matched_by_tool_name = {
                name: upstream
                for name, upstream in upstreams.items()
                if upstream_owns_tool_name(
                    upstream,
                    tool_name,
                    self._broker_config.broker.tool_namespace_separator,
                )
            }
            if matched_by_tool_name:
                return matched_by_tool_name
        if not _specific_query_can_select_upstream(query):
            return upstreams
        matched = {
            name: upstream
            for name, upstream in upstreams.items()
            if upstream_metadata_matches(upstream, query)
        }
        return matched or upstreams


def _snapshot_int(snapshot: dict[str, object], *keys: str) -> int:
    for key in keys:
        value = snapshot.get(key)
        if isinstance(value, int):
            return value
    return 0


def _auth_state(snapshot: dict[str, object]) -> str:
    value = snapshot.get("auth_state")
    if value in {"authenticated", "unauthenticated", "unknown"}:
        return str(value)
    last_error = snapshot.get("last_error")
    if isinstance(last_error, str) and _looks_like_auth_error(last_error):
        return "unauthenticated"
    return "unknown"


def _looks_like_auth_error(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in (
            "auth",
            "credential",
            "forbidden",
            "token",
            "unauthorized",
            "401",
            "403",
        )
    )


def _broker_status(upstreams: dict[str, dict[str, Any]]) -> str:
    for upstream in upstreams.values():
        if upstream.get("last_error"):
            return "degraded"
        if upstream.get("state") in {"exited", "failed", "backoff"}:
            return "degraded"
    return "ok"


def profile_allows_upstream(
    profile: ToolExposureProfile | None,
    upstream: UpstreamConfig,
) -> bool:
    if profile is None:
        return True
    return profile.name in upstream.profiles


def catalog_entries_for_upstream(
    upstream: UpstreamConfig,
    tools: list[dict[str, object]],
    separator: str,
) -> list[dict[str, Any]]:
    prefix = upstream.tool_prefix or upstream.name
    entries = []
    for tool in tools:
        tool_name = str(tool.get("name", ""))
        if not tool_name:
            continue
        entries.append(
            {
                "name": f"{prefix}{separator}{tool_name}",
                "upstream": upstream.name,
                "description": str(tool.get("description", "")),
                "inputSchema": tool.get("inputSchema", {"type": "object"}),
                "purpose": upstream.purpose,
                "tags": list(upstream.tags),
                "mutating": upstream.mutating,
            }
        )
    return entries


def slim_catalog_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Search-result view of a catalog entry, without the heavy ``inputSchema``.

    Search exists to let the client pick a tool, which only needs name, upstream,
    description, purpose, tags, mutating, and availability. The exact ``inputSchema``
    is the single largest field and is fetched on demand with ``broker_describe_tool``
    right before a call. Dropping it from search results is the biggest token saving
    on the discovery path. Relevance scoring runs on the full entry before this slim,
    so omitting the schema here does not change which tools rank or match. A new dict
    is returned so the source entry the describe path reuses is never mutated.
    """
    return {key: value for key, value in entry.items() if key != "inputSchema"}


def catalog_unavailable_entry_for_upstream(
    upstream: UpstreamConfig,
    error: str,
) -> dict[str, Any]:
    return {
        "name": upstream.name,
        "upstream": upstream.name,
        "description": f"upstream unavailable: {error}",
        "purpose": upstream.purpose,
        "tags": list(upstream.tags),
        "mutating": upstream.mutating,
        "available": False,
    }


# Relevance weights for tool discovery. A query token in the tool name / upstream /
# tag is the strongest signal, the purpose is mid, the description is weakest (prose
# mentions are noisy). Single source of truth so the tests assert against the same
# constants the code reads.
_SCORE_NAME = 3
_SCORE_PURPOSE = 2
_SCORE_DESCRIPTION = 1


def catalog_entry_score(entry: dict[str, Any], query: str) -> int:
    """Relevance score for an entry against a query (token overlap, weighted by field).

    Each query token contributes its single strongest field hit, summed across
    tokens. An empty query scores every entry equally (non-zero) so discovery with
    no filter returns the full catalog. Tokens that match nothing add nothing, so a
    natural-language query still ranks the relevant tools above the noise instead of
    returning nothing the moment one word is absent.
    """
    tokens = query.lower().split()
    if not tokens:
        return _SCORE_NAME
    name_fields = [
        str(entry.get("name", "")).lower(),
        str(entry.get("upstream", "")).lower(),
    ]
    name_fields.extend(str(tag).lower() for tag in entry.get("tags", []))
    purpose = str(entry.get("purpose", "")).lower()
    description = str(entry.get("description", "")).lower()
    score = 0
    for token in tokens:
        if any(token in field for field in name_fields):
            score += _SCORE_NAME
        elif token in purpose:
            score += _SCORE_PURPOSE
        elif token in description:
            score += _SCORE_DESCRIPTION
    return score


def catalog_entry_matches(entry: dict[str, Any], query: str) -> bool:
    return catalog_entry_score(entry, query) > 0


def upstream_metadata_matches(upstream: UpstreamConfig, query: str) -> bool:
    smoke_query = upstream.smoke.query if upstream.smoke is not None else ""
    smoke_tool = upstream.smoke.tool if upstream.smoke is not None else ""
    prefix = upstream.tool_prefix or upstream.name
    return catalog_entry_matches(
        {
            "name": f"{upstream.name} {prefix} {smoke_tool}",
            "description": smoke_query,
            "purpose": upstream.purpose,
            "tags": list(upstream.tags),
        },
        query,
    )


def upstream_owns_tool_name(upstream: UpstreamConfig, tool_name: str, separator: str) -> bool:
    prefix = upstream.tool_prefix or upstream.name
    return tool_name.startswith(f"{prefix}{separator}")


def _specific_query_can_select_upstream(query: str) -> bool:
    return len(query.split()) >= 2


def structured_tool_result(structured_content: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured_content, sort_keys=True),
            }
        ],
        "structuredContent": structured_content,
    }


def project_value(
    value: Any,
    paths: list[str] | None,
    max_array_items: int | None,
) -> Any:
    """Prune a JSON value to the requested dotted ``paths`` and array cap.

    A path component walks into a dict by key. When a path reaches a list, the
    remaining path is applied to every element of that list, so ``items.id`` keeps
    only ``id`` from each item. A fully consumed path keeps the whole subtree below
    it. Missing keys are skipped. With no paths, every key is kept; ``max_array_items``
    still truncates every list anywhere in the result so a caller can cap a large
    response without naming fields. Scalars and unmatched branches are returned
    unchanged. The input is never mutated - new containers are built as we descend.
    """
    return _project(value, _build_path_tree(paths) if paths else None, max_array_items)


def _project(value: Any, tree: dict[str, Any] | None, cap: int | None) -> Any:
    if isinstance(value, list):
        projected = [_project(item, tree, cap) for item in value]
        if cap is not None:
            return projected[:cap]
        return projected
    if isinstance(value, dict):
        if not tree:
            # No selector remains below here, so keep every key. Recurse with an
            # explicit None (not the falsy `tree`) so the cap still applies to nested
            # lists and there is no equivalent tree-to-None mutation on this line.
            return {key: _project(item, None, cap) for key, item in value.items()}
        result: dict[str, Any] = {}
        for key, subtree in tree.items():
            if key in value:
                result[key] = _project(value[key], subtree, cap)
        return result
    return value


def _build_path_tree(paths: list[str]) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for path in paths:
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})
    return tree


def apply_projection(response: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
    """Apply a caller-supplied projection to an upstream tools/call response.

    Returns the response unchanged when the projection selects nothing (no paths and
    no cap). Otherwise it prunes the structured payload and re-serializes the text
    block so the trimmed payload is exactly what the client sees, or - when there is
    no ``structuredContent`` - prunes any JSON-decodable text block while leaving
    non-JSON text untouched. A ``_meta.projection`` note records what happened. The
    source response is copied, never mutated.
    """
    paths, cap = _normalize_projection(projection)
    if not paths and cap is None:
        return response
    projected = deepcopy(response)
    applied = False
    structured = projected.get("structuredContent")
    if isinstance(structured, (dict, list)):
        pruned = project_value(structured, paths, cap)
        projected["structuredContent"] = pruned
        projected["content"] = [{"type": "text", "text": json.dumps(pruned, sort_keys=True)}]
        applied = True
    else:
        new_content = []
        existing = projected.get("content")
        blocks = existing if isinstance(existing, list) else []
        for block in blocks:
            pruned_block = _project_text_block(block, paths, cap)
            if pruned_block is None:
                new_content.append(block)
            else:
                new_content.append(pruned_block)
                applied = True
        projected["content"] = new_content
    projected.setdefault("_meta", {})["projection"] = {
        "applied": applied,
        "paths": list(paths) if paths else [],
        "max_array_items": cap,
    }
    return projected


def _project_text_block(
    block: Any,
    paths: list[str] | None,
    cap: int | None,
) -> dict[str, Any] | None:
    if not (
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ):
        return None
    try:
        parsed = json.loads(block["text"])
    except (ValueError, TypeError):
        return None
    return {**block, "text": json.dumps(project_value(parsed, paths, cap), sort_keys=True)}


def _normalize_projection(projection: dict[str, Any]) -> tuple[list[str] | None, int | None]:
    if not isinstance(projection, dict):
        raise ValueError("projection must be an object")
    unknown = set(projection) - {"paths", "max_array_items"}
    if unknown:
        raise ValueError(f"projection has unknown keys: {sorted(unknown)}")
    paths = projection.get("paths")
    if paths is not None:
        if not isinstance(paths, list) or not all(isinstance(part, str) for part in paths):
            raise ValueError("projection.paths must be a list of strings")
        paths = [part for part in paths if part]
    cap = projection.get("max_array_items")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        raise ValueError("projection.max_array_items must be a non-negative integer")
    return (paths or None, cap)
