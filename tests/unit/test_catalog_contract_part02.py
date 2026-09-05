import json
from pathlib import Path
import pytest
from mcp_broker.catalog import (
    BrokerCatalogFacade,
    _specific_query_can_select_upstream,
    catalog_entries_for_upstream,
    catalog_entry_matches,
    catalog_unavailable_entry_for_upstream,
    profile_allows_upstream,
    structured_tool_result,
    upstream_metadata_matches,
    upstream_owns_tool_name,
)
from mcp_broker.config import (
    BrokerConfig,
    BrokerSettings,
    RuntimeConfig,
    SmokeProbe,
    UpstreamConfig,
)
from mcp_broker.profiles import ToolExposureProfile
pytestmark = pytest.mark.unit
def _projection_error_message(projection: object) -> str:
    from mcp_broker.catalog import apply_projection

    with pytest.raises(ValueError) as exc:
        apply_projection({"content": []}, projection)  # type: ignore[arg-type]
    return str(exc.value)
def _catalog_config(tmp_path: Path) -> BrokerConfig:
    return BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={
            "default-llm": ToolExposureProfile(
                name="default-llm",
                max_tools=20,
                compact_tools_enabled=True,
                allow_mutating_upstreams=("write-store",),
            ),
            "other-llm": ToolExposureProfile(name="other-llm", max_tools=20),
        },
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
                purpose="Read records",
                tags=("records",),
            ),
            "write-store": UpstreamConfig(
                name="write-store",
                command="write-store",
                tool_prefix="write",
                profiles=("default-llm",),
                mutating=True,
            ),
            "broken-store": UpstreamConfig(
                name="broken-store",
                command="broken-store",
                profiles=("default-llm",),
            ),
            "other-profile-store": UpstreamConfig(
                name="other-profile-store",
                command="other-profile-store",
                profiles=("other-llm",),
            ),
            "disabled-store": UpstreamConfig(
                name="disabled-store",
                command="disabled-store",
                enabled=False,
                profiles=("default-llm",),
            ),
        },
    )
def _runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        log_dir=tmp_path / "runtime" / "logs",
        state_dir=tmp_path / "runtime" / "state",
        secrets_dir=tmp_path / "runtime" / "secrets",
    )

def test_apply_projection_rejects_invalid_projection_shapes() -> None:
    assert (
        _projection_error_message({"paths": "id"})
        == "projection.paths must be a list of strings"
    )
    assert (
        _projection_error_message({"paths": [1]})
        == "projection.paths must be a list of strings"
    )
    assert (
        _projection_error_message({"max_array_items": -1})
        == "projection.max_array_items must be a non-negative integer"
    )
    assert (
        _projection_error_message({"max_array_items": True})
        == "projection.max_array_items must be a non-negative integer"
    )
    assert _projection_error_message({"unknown": 1}) == "projection has unknown keys: ['unknown']"

def test_call_tool_applies_projection_to_upstream_response(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    verbose = {"id": 7, "blob": "x" * 500, "items": [{"id": 1, "noise": "n"}]}

    def call_upstream(_name, _tool, _args, _timeout):
        return {
            "content": [{"type": "text", "text": json.dumps(verbose)}],
            "structuredContent": verbose,
        }

    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=call_upstream,
        call_locks={},
    )

    result = facade.call_tool(
        "broker.call_tool",
        {
            "name": "read.find_record",
            "arguments": {},
            "projection": {"paths": ["id", "items.id"]},
        },
    )

    assert result["structuredContent"] == {"id": 7, "items": [{"id": 1}]}
    assert result["_meta"]["projection"]["applied"] is True

def test_call_tool_without_projection_returns_full_response(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    full = {"id": 7, "blob": "x" * 50}

    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {
            "content": [{"type": "text", "text": json.dumps(full)}],
            "structuredContent": full,
        },
        call_locks={},
    )

    result = facade.call_tool("broker.call_tool", {"name": "read.find_record", "arguments": {}})

    assert result["structuredContent"] == full
    assert "_meta" not in result

def test_call_tool_rejects_non_object_projection(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(ValueError) as exc:
        facade.call_tool(
            "broker.call_tool",
            {"name": "read.find_record", "arguments": {}, "projection": "id"},
        )
    # Exact message so the message-string mutations on this raise are killed.
    assert str(exc.value) == "broker.call_tool projection must be an object"

def test_structured_tool_result_returns_exact_mcp_payload_shape() -> None:
    payload = {"z": 1, "a": 2}

    assert structured_tool_result(payload) == {
        "content": [
            {
                "type": "text",
                "text": '{"a": 2, "z": 1}',
            }
        ],
        "structuredContent": payload,
    }

def test_profile_allows_upstream_without_profile_or_with_matching_profile() -> None:
    upstream = UpstreamConfig(
        name="read-store",
        command="read-store",
        profiles=("default-llm",),
    )

    assert profile_allows_upstream(None, upstream)
    assert profile_allows_upstream(ToolExposureProfile(name="default-llm", max_tools=20), upstream)
    assert not profile_allows_upstream(ToolExposureProfile(name="other-llm", max_tools=20), upstream)

def test_search_tools_returns_limited_matches_and_skipped_upstreams(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    list_calls: list[tuple[str, int]] = []

    def list_upstream(upstream_name: str, timeout: int) -> list[dict[str, object]]:
        list_calls.append((upstream_name, timeout))
        if upstream_name == "broken-store":
            raise RuntimeError("missing runtime token")
        if upstream_name == "read-store":
            return [
                {"name": "find_record", "description": "Find a record"},
                {"name": "list_records", "description": "List records"},
            ]
        return [{"name": "ignored"}]

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "record", "limit": "1"})

    assert result["structuredContent"] == {
        "matches": [
            {
                "name": "read.find_record",
                "upstream": "read-store",
                "description": "Find a record",
                "purpose": "Read records",
                "tags": ["records"],
                "mutating": False,
            }
        ],
        "skipped_upstreams": {"broken-store": "missing runtime token"},
    }
    # Search results omit inputSchema - the heavy field is fetched on demand via
    # broker_describe_tool. Every match still carries the discovery signal.
    assert "inputSchema" not in result["structuredContent"]["matches"][0]
    assert list_calls == [("read-store", 60), ("broken-store", 60)]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

def test_search_tools_excludes_entries_with_zero_relevance_score(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "calendar"})

    assert result["structuredContent"]["matches"] == []

def test_catalog_entries_preserve_upstream_error_in_unavailable_entry(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "broken-store": UpstreamConfig(
                name="broken-store",
                command="broken-store",
                profiles=("default-llm",),
            )
        },
    )
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: (_ for _ in ()).throw(
            RuntimeError("missing runtime token")
        ),
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    entries, skipped = facade._catalog_entries(query="", tool_name="")

    assert skipped == {"broken-store": "missing runtime token"}
    assert entries[0]["description"] == "upstream unavailable: missing runtime token"

def test_catalog_entries_requires_explicit_query_or_tool_name(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(TypeError):
        facade._catalog_entries()  # type: ignore[call-arg]

def test_catalog_entries_rejects_invalid_selector_modes(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(TypeError) as query_type_error:
        facade._catalog_entries(query="record", tool_name=None)  # type: ignore[arg-type]
    assert str(query_type_error.value) == "query and tool_name must be strings"

    with pytest.raises(TypeError) as tool_name_type_error:
        facade._catalog_entries(query=None, tool_name="read.find_record")  # type: ignore[arg-type]
    assert str(tool_name_type_error.value) == "query and tool_name must be strings"

    with pytest.raises(ValueError) as double_selector_error:
        facade._catalog_entries(query="record", tool_name="read.find_record")
    assert str(double_selector_error.value) == "use query or tool_name, not both"

def test_catalog_upstreams_requires_explicit_query_or_tool_name(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [{"name": "find_record"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(TypeError):
        facade._catalog_upstreams()  # type: ignore[call-arg]

def test_search_tools_defaults_to_empty_query_and_twenty_results(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=30)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [
            {"name": f"tool_{index:02d}", "description": f"Tool {index:02d}"}
            for index in range(21)
        ],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {})

    names = [match["name"] for match in result["structuredContent"]["matches"]]
    assert names == [f"read.tool_{index:02d}" for index in range(20)]

def test_search_tools_uses_upstream_metadata_to_avoid_slow_irrelevant_listing(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=80)},
        upstreams={
            "notes-cache": UpstreamConfig(
                name="notes-cache",
                command="notes-cache",
                tool_prefix="notes-cache",
                profiles=("codex",),
                purpose="Persistent project notes and cross-session context.",
                tags=("notes", "context", "project-context"),
            ),
            "repo-index": UpstreamConfig(
                name="repo-index",
                command="repo-index",
                tool_prefix="repo-index",
                profiles=("codex",),
                purpose="Codebase graph exploration, architecture lookup, and call tracing.",
                tags=("codebase", "graph", "architecture", "tracing"),
                smoke=SmokeProbe(
                    query="list indexed codebase projects",
                    tool="repo-index.list_projects",
                    arguments={},
                ),
            ),
        },
    )
    list_calls: list[str] = []

    def list_upstream(upstream_name: str, _timeout: int) -> list[dict[str, object]]:
        list_calls.append(upstream_name)
        if upstream_name == "notes-cache":
            raise RuntimeError("notes-cache should not be listed for this query")
        return [{"name": "list_projects", "description": "List indexed projects"}]

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "list indexed codebase projects"})

    assert list_calls == ["repo-index"]
    assert [match["name"] for match in result["structuredContent"]["matches"]] == [
        "repo-index.list_projects"
    ]

def test_search_tools_keeps_all_upstreams_for_single_token_queries(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=80)},
        upstreams={
            "notes-cache": UpstreamConfig(
                name="notes-cache",
                command="notes-cache",
                tool_prefix="notes",
                profiles=("codex",),
                purpose="Persistent project notes",
                tags=("context",),
            ),
            "repo-index": UpstreamConfig(
                name="repo-index",
                command="repo-index",
                tool_prefix="repo",
                profiles=("codex",),
                purpose="Codebase graph exploration",
                tags=("codebase",),
            ),
        },
    )
    list_calls: list[str] = []

    BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda name, _timeout: list_calls.append(name) or [{"name": "search"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "codebase"})

    assert list_calls == ["notes-cache", "repo-index"]

def test_search_tools_falls_back_to_all_upstreams_when_metadata_has_no_match(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=80)},
        upstreams={
            "notes-cache": UpstreamConfig(
                name="notes-cache",
                command="notes-cache",
                tool_prefix="notes",
                profiles=("codex",),
                purpose="Persistent project notes",
                tags=("context",),
            ),
            "repo-index": UpstreamConfig(
                name="repo-index",
                command="repo-index",
                tool_prefix="repo",
                profiles=("codex",),
                purpose="Codebase graph exploration",
                tags=("codebase",),
            ),
        },
    )
    list_calls: list[str] = []

    BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda name, _timeout: list_calls.append(name) or [{"name": "search"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "calendar event"})

    assert list_calls == ["notes-cache", "repo-index"]

def test_describe_tool_returns_exact_catalog_entry_and_rejects_bad_names(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [{"name": "find_record", "description": "Find"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    described = facade.call_tool("broker.describe_tool", {"name": "read.find_record"})

    assert described["structuredContent"]["tool"]["name"] == "read.find_record"
    assert described["structuredContent"]["tool"]["description"] == "Find"
    with pytest.raises(ValueError) as invalid_name:
        facade.call_tool("broker.describe_tool", {"name": 123})
    assert str(invalid_name.value) == "broker.describe_tool requires string name"
    with pytest.raises(ValueError, match="broker tool not found"):
        facade.call_tool("broker.describe_tool", {"name": "read.missing"})

def test_describe_tool_uses_tool_prefix_to_avoid_slow_irrelevant_listing(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=80)},
        upstreams={
            "notes-cache": UpstreamConfig(
                name="notes-cache",
                command="notes-cache",
                tool_prefix="notes-cache",
                profiles=("codex",),
            ),
            "repo-index": UpstreamConfig(
                name="repo-index",
                command="repo-index",
                tool_prefix="repo-index",
                profiles=("codex",),
            ),
        },
    )
    list_calls: list[str] = []

    def list_upstream(upstream_name: str, _timeout: int) -> list[dict[str, object]]:
        list_calls.append(upstream_name)
        if upstream_name == "notes-cache":
            raise RuntimeError("notes-cache should not be listed for this tool")
        return [{"name": "list_projects", "description": "List indexed projects"}]

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.describe_tool", {"name": "repo-index.list_projects"})

    assert list_calls == ["repo-index"]
    assert result["structuredContent"]["tool"]["name"] == "repo-index.list_projects"

def test_describe_tool_falls_back_to_all_upstreams_for_unknown_prefix(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=80)},
        upstreams={
            "notes-cache": UpstreamConfig(
                name="notes-cache",
                command="notes-cache",
                tool_prefix="notes",
                profiles=("codex",),
            ),
            "repo-index": UpstreamConfig(
                name="repo-index",
                command="repo-index",
                tool_prefix="repo",
                profiles=("codex",),
            ),
        },
    )
    list_calls: list[str] = []

    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda name, _timeout: list_calls.append(name) or [{"name": "search"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(ValueError, match="broker tool not found"):
        facade.call_tool("broker.describe_tool", {"name": "unknown.search"})

    assert list_calls == ["notes-cache", "repo-index"]

def test_call_tool_accepts_profile_snake_aliases(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    profile = ToolExposureProfile(
        name="default-llm",
        max_tools=20,
        broker_tool_name_style="snake",
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=profile,
        list_upstream=lambda _name, _timeout: [{"name": "find_record", "description": "Find"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker_search_tools", {"query": "find"})

    match_names = [match["name"] for match in result["structuredContent"]["matches"]]
    assert "read.find_record" in match_names

def test_search_tools_ranks_name_matches_above_description_matches(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path,
            socket_path=tmp_path / "s.sock",
            log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state",
            secrets_dir=tmp_path / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={"llm": ToolExposureProfile(name="llm", max_tools=20)},
        upstreams={
            "alpha": UpstreamConfig(name="alpha", command="alpha", tool_prefix="a", profiles=("llm",)),
            "beta": UpstreamConfig(name="beta", command="beta", tool_prefix="b", profiles=("llm",)),
        },
    )

    def list_upstream(name: str, _timeout: int) -> list[dict[str, object]]:
        if name == "alpha":
            return [{"name": "deploy_app", "description": "unrelated text"}]  # name hit -> high score
        return [{"name": "run", "description": "deploy the app"}]  # description hit -> low score

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["llm"],
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "deploy"})

    names = [match["name"] for match in result["structuredContent"]["matches"]]
    # Name match (a.deploy_app) outranks the description-only match (b.run).
    assert names == ["a.deploy_app", "b.run"]
