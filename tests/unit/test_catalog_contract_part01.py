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

def test_catalog_entry_matching_uses_any_token_relevance() -> None:
    entry = {
        "name": "work-store.search_items",
        "upstream": "work-store",
        "description": "Search project records",
        "purpose": "Project collaboration",
        "tags": ["records", "read-only"],
    }

    assert catalog_entry_matches(entry, "")
    assert catalog_entry_matches(entry, "work-store records")
    assert catalog_entry_matches(entry, "SEARCH project")
    # A partial natural-language query still matches on its present tokens, instead
    # of returning nothing the moment one word ("missing") is absent.
    assert catalog_entry_matches(entry, "work-store missing")
    assert not catalog_entry_matches(entry, "unknown")

def test_catalog_entry_score_weights_name_over_purpose_over_description() -> None:
    from mcp_broker.catalog import (
        _SCORE_DESCRIPTION,
        _SCORE_NAME,
        _SCORE_PURPOSE,
        catalog_entry_score,
    )

    assert _SCORE_NAME > _SCORE_PURPOSE > _SCORE_DESCRIPTION
    assert catalog_entry_score({"name": "deploy"}, "deploy") == _SCORE_NAME
    assert catalog_entry_score({"tags": ["deploy"]}, "deploy") == _SCORE_NAME
    assert catalog_entry_score({"purpose": "deploy"}, "deploy") == _SCORE_PURPOSE
    assert catalog_entry_score({"description": "deploy"}, "deploy") == _SCORE_DESCRIPTION
    # Each token counts its single strongest field, not every field it appears in.
    assert catalog_entry_score({"name": "deploy", "description": "deploy"}, "deploy") == _SCORE_NAME
    # Scores accumulate across matching tokens; absent tokens add nothing.
    assert catalog_entry_score({"name": "fly deploy"}, "fly deploy nonsense") == 2 * _SCORE_NAME
    # Different tiers accumulate (a prior token's score is added to, not overwritten).
    tiered = {"name": "alpha", "purpose": "bravo", "description": "charlie"}
    assert catalog_entry_score(tiered, "alpha bravo") == _SCORE_NAME + _SCORE_PURPOSE
    assert catalog_entry_score(tiered, "alpha charlie") == _SCORE_NAME + _SCORE_DESCRIPTION
    # Empty query is a uniform non-zero score (full catalog passes the filter).
    assert catalog_entry_score({"name": "x"}, "") == _SCORE_NAME
    assert catalog_entry_score({}, "missing") == 0

@pytest.mark.parametrize(
    "query",
    ["alpha-tool", "beta-upstream", "gamma-description", "delta-purpose", "epsilon-tag"],
)
def test_catalog_entry_matching_indexes_each_catalog_field(query: str) -> None:
    entry = {
        "name": "alpha-tool",
        "upstream": "beta-upstream",
        "description": "gamma-description",
        "purpose": "delta-purpose",
        "tags": ["epsilon-tag", "zeta-tag"],
    }

    assert catalog_entry_matches(entry, query)

def test_catalog_entry_matching_does_not_index_missing_field_defaults() -> None:
    assert not catalog_entry_matches({}, "none")
    assert not catalog_entry_matches({}, "xxxx")
    assert not catalog_entry_matches({"tags": ["read-only"]}, "xx")
    # Present tokens match even when other tokens ("xx") are absent.
    assert catalog_entry_matches(
        {"tags": ["epsilon-tag", "zeta-tag"]},
        "epsilon-tag xx zeta-tag",
    )

def test_upstream_metadata_matching_indexes_identity_prefix_smoke_purpose_and_tags() -> None:
    # Distinct tokens per field so a single-token query isolates exactly one field;
    # if any field stops being indexed, its query stops matching.
    upstream = UpstreamConfig(
        name="alphaname",
        command="alphaname",
        tool_prefix="bravoprefix",
        purpose="charliepurpose graph",
        tags=("deltatag", "echotag"),
        smoke=SmokeProbe(
            query="foxtrotquery indexed",
            tool="golftool",
            arguments={},
        ),
    )

    assert upstream_metadata_matches(upstream, "alphaname")  # upstream name
    assert upstream_metadata_matches(upstream, "bravoprefix")  # tool prefix
    assert upstream_metadata_matches(upstream, "golftool")  # smoke tool name
    assert upstream_metadata_matches(upstream, "foxtrotquery")  # smoke query (description)
    assert upstream_metadata_matches(upstream, "charliepurpose")  # purpose
    assert upstream_metadata_matches(upstream, "deltatag")  # tag
    assert upstream_metadata_matches(upstream, "echotag")  # tag
    # A query whose tokens hit no field does not match.
    assert not upstream_metadata_matches(upstream, "nonexistent")

def test_upstream_metadata_matching_handles_missing_smoke_and_prefix_fallback() -> None:
    upstream = UpstreamConfig(
        name="notes-cache",
        command="notes-cache",
        tool_prefix=None,
        purpose="Persistent notes",
        tags=("context",),
    )

    assert upstream_metadata_matches(upstream, "notes-cache context")
    assert upstream_metadata_matches(upstream, "persistent notes")
    assert not upstream_metadata_matches(upstream, "list projects")
    assert not upstream_metadata_matches(upstream, "xxxx")

def test_upstream_metadata_matching_indexes_custom_prefix_without_smoke() -> None:
    upstream = UpstreamConfig(
        name="repo-index",
        command="repo-index",
        tool_prefix="codegraph",
        purpose="",
        tags=(),
    )

    assert upstream_metadata_matches(upstream, "codegraph")

def test_upstream_tool_name_matching_requires_prefix_and_separator() -> None:
    prefixed = UpstreamConfig(name="repo-index", command="repo-index", tool_prefix="repo")
    fallback = UpstreamConfig(name="notes-cache", command="notes-cache", tool_prefix=None)

    assert upstream_owns_tool_name(prefixed, "repo.list_projects", ".")
    assert upstream_owns_tool_name(fallback, "notes-cache__search", "__")
    assert not upstream_owns_tool_name(prefixed, "repo-index.list_projects", ".")
    assert not upstream_owns_tool_name(prefixed, "repo-list_projects", ".")
    assert not upstream_owns_tool_name(prefixed, "xrepo.list_projects", ".")

@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", False),
        ("github", False),
        (" github ", False),
        ("github issue", True),
        ("  github   issue  ", True),
    ],
)
def test_specific_query_requires_at_least_two_tokens(query: str, expected: bool) -> None:
    assert _specific_query_can_select_upstream(query) is expected

def test_catalog_entries_use_prefix_schema_metadata_and_skip_nameless_tools() -> None:
    upstream = UpstreamConfig(
        name="work-store",
        command="work-store",
        tool_prefix="work",
        purpose="Search work records",
        tags=("records", "read-only"),
        mutating=True,
    )

    entries = catalog_entries_for_upstream(
        upstream,
        [
            {"description": "no tool name"},
            {
                "name": "lookup",
                "description": "Lookup a record",
                "inputSchema": {"type": "object", "required": ["id"]},
            },
            {"name": "health"},
        ],
        ".",
    )

    assert entries == [
        {
            "name": "work.lookup",
            "upstream": "work-store",
            "description": "Lookup a record",
            "inputSchema": {"type": "object", "required": ["id"]},
            "purpose": "Search work records",
            "tags": ["records", "read-only"],
            "mutating": True,
        },
        {
            "name": "work.health",
            "upstream": "work-store",
            "description": "",
            "inputSchema": {"type": "object"},
            "purpose": "Search work records",
            "tags": ["records", "read-only"],
            "mutating": True,
        },
    ]

def test_catalog_entries_fall_back_to_upstream_name_when_prefix_is_empty() -> None:
    upstream = UpstreamConfig(name="read-store", command="read-store", tool_prefix=None)

    entries = catalog_entries_for_upstream(upstream, [{"name": "read"}], "__")

    assert entries[0]["name"] == "read-store__read"
    assert entries[0]["upstream"] == "read-store"

def test_unavailable_catalog_entry_keeps_upstream_metadata() -> None:
    upstream = UpstreamConfig(
        name="remote-store",
        command="remote-store",
        purpose="Remote records",
        tags=("remote",),
        mutating=True,
    )

    assert catalog_unavailable_entry_for_upstream(upstream, "missing token") == {
        "name": "remote-store",
        "upstream": "remote-store",
        "description": "upstream unavailable: missing token",
        "purpose": "Remote records",
        "tags": ["remote"],
        "mutating": True,
        "available": False,
    }

def test_slim_catalog_entry_drops_input_schema_keeps_discovery_signal() -> None:
    from mcp_broker.catalog import slim_catalog_entry

    entry = {
        "name": "work.lookup",
        "upstream": "work-store",
        "description": "Lookup a record",
        "inputSchema": {"type": "object", "required": ["id"]},
        "purpose": "Project records",
        "tags": ["records"],
        "mutating": True,
    }

    slim = slim_catalog_entry(entry)

    assert slim == {
        "name": "work.lookup",
        "upstream": "work-store",
        "description": "Lookup a record",
        "purpose": "Project records",
        "tags": ["records"],
        "mutating": True,
    }
    # The schema is the heavy field; it is the only thing dropped.
    assert "inputSchema" not in slim
    # The source entry is not mutated - describe still needs the full entry.
    assert entry["inputSchema"] == {"type": "object", "required": ["id"]}

def test_slim_catalog_entry_preserves_unavailable_stub_fields() -> None:
    from mcp_broker.catalog import slim_catalog_entry

    stub = {
        "name": "remote-store",
        "upstream": "remote-store",
        "description": "upstream unavailable: missing token",
        "purpose": "Remote records",
        "tags": ["remote"],
        "mutating": True,
        "available": False,
    }

    assert slim_catalog_entry(stub) == stub

def test_describe_tool_returns_full_input_schema_after_search_slims_it(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)

    def list_upstream(upstream_name: str, timeout: int) -> list[dict[str, object]]:
        if upstream_name == "read-store":
            return [
                {
                    "name": "find_record",
                    "description": "Find a record",
                    "inputSchema": {"type": "object", "required": ["id"]},
                }
            ]
        return []

    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    search = facade.call_tool("broker.search_tools", {"query": "record"})
    assert "inputSchema" not in search["structuredContent"]["matches"][0]

    described = facade.call_tool("broker.describe_tool", {"name": "read.find_record"})
    assert described["structuredContent"]["tool"]["inputSchema"] == {
        "type": "object",
        "required": ["id"],
    }

def test_project_value_keeps_only_requested_dotted_paths() -> None:
    from mcp_broker.catalog import project_value

    payload = {
        "data": {"id": 1, "secret": "x", "nested": {"keep": 9, "drop": 0}},
        "noise": [1, 2, 3],
    }

    assert project_value(payload, ["data.id", "data.nested.keep"], None) == {
        "data": {"id": 1, "nested": {"keep": 9}}
    }

def test_project_value_maps_remaining_path_over_list_elements() -> None:
    from mcp_broker.catalog import project_value

    payload = {"items": [{"id": 1, "big": "a"}, {"id": 2, "big": "b"}], "cursor": "c"}

    assert project_value(payload, ["items.id", "cursor"], None) == {
        "items": [{"id": 1}, {"id": 2}],
        "cursor": "c",
    }

def test_project_value_leaf_path_keeps_whole_subtree() -> None:
    from mcp_broker.catalog import project_value

    payload = {"item": {"id": 1, "name": "x"}, "drop": True}

    assert project_value(payload, ["item"], None) == {"item": {"id": 1, "name": "x"}}

def test_project_value_skips_missing_keys() -> None:
    from mcp_broker.catalog import project_value

    assert project_value({"a": 1}, ["a", "missing.deep"], None) == {"a": 1}

def test_project_value_caps_arrays_with_max_array_items_keeping_all_keys() -> None:
    from mcp_broker.catalog import project_value

    payload = {"rows": [{"a": 1}, {"a": 2}, {"a": 3}], "total": 3}

    # No paths + a cap keeps every field but truncates lists everywhere.
    assert project_value(payload, None, 2) == {"rows": [{"a": 1}, {"a": 2}], "total": 3}

def test_project_value_cap_applies_to_nested_lists_under_projected_paths() -> None:
    from mcp_broker.catalog import project_value

    payload = {"groups": [{"tags": ["x", "y", "z"]}]}

    assert project_value(payload, ["groups.tags"], 1) == {"groups": [{"tags": ["x"]}]}

def test_project_value_returns_scalars_unchanged() -> None:
    from mcp_broker.catalog import project_value

    assert project_value(7, ["a"], None) == 7
    assert project_value("text", ["a"], 1) == "text"

def test_apply_projection_prunes_structured_content_and_resyncs_text() -> None:
    from mcp_broker.catalog import apply_projection

    response = {
        "content": [{"type": "text", "text": json.dumps({"b": 2, "a": 1, "blob": "x" * 100})}],
        "structuredContent": {"b": 2, "a": 1, "blob": "x" * 100},
    }

    projected = apply_projection(response, {"paths": ["b", "a"]})

    assert projected["structuredContent"] == {"b": 2, "a": 1}
    # Assert the EXACT content block: a wrong "type"/"text" key or value, or losing
    # sort_keys=True (which would emit {"b":2,"a":1} insertion order), all fail here.
    assert projected["content"] == [{"type": "text", "text": '{"a": 1, "b": 2}'}]
    assert projected["_meta"]["projection"] == {
        "applied": True,
        "paths": ["b", "a"],
        "max_array_items": None,
    }
    # The source response is never mutated.
    assert response["structuredContent"] == {"b": 2, "a": 1, "blob": "x" * 100}

def test_apply_projection_text_block_resyncs_sorted_multikey_json() -> None:
    from mcp_broker.catalog import apply_projection

    # No structuredContent: prune the JSON text block. Multi-key + exact string so
    # the sort_keys=True serialization is asserted (kills sort_keys mutations).
    response = {"content": [{"type": "text", "text": json.dumps({"b": 2, "a": 1, "drop": 3})}]}

    projected = apply_projection(response, {"paths": ["b", "a"]})

    assert projected["content"][0]["text"] == '{"a": 1, "b": 2}'

def test_apply_projection_with_no_structured_content_or_content_list() -> None:
    from mcp_broker.catalog import apply_projection

    # No structuredContent and no content list: the content fallback is the empty
    # list, projection applies to nothing, and a _meta note is still recorded.
    projected = apply_projection({}, {"paths": ["id"]})

    assert projected["content"] == []
    assert projected["_meta"]["projection"]["applied"] is False

def test_apply_projection_text_block_applies_cap() -> None:
    from mcp_broker.catalog import apply_projection

    # cap must reach the text-block path (no structuredContent): a dropped cap here
    # would leave the array untruncated.
    response = {"content": [{"type": "text", "text": json.dumps({"rows": [1, 2, 3, 4]})}]}

    projected = apply_projection(response, {"max_array_items": 2})

    assert json.loads(projected["content"][0]["text"]) == {"rows": [1, 2]}

def test_apply_projection_is_a_noop_without_paths_or_cap() -> None:
    from mcp_broker.catalog import apply_projection

    response = {"content": [], "structuredContent": {"id": 1}}

    result = apply_projection(response, {})

    assert result == response
    assert "_meta" not in result

def test_apply_projection_prunes_json_text_block_without_structured_content() -> None:
    from mcp_broker.catalog import apply_projection

    response = {
        "content": [
            {"type": "text", "text": json.dumps({"id": 1, "blob": "x" * 50})},
            {"type": "text", "text": "not json, left alone"},
        ]
    }

    projected = apply_projection(response, {"paths": ["id"]})

    assert json.loads(projected["content"][0]["text"]) == {"id": 1}
    assert projected["content"][1]["text"] == "not json, left alone"
    assert projected["_meta"]["projection"]["applied"] is True

def test_apply_projection_marks_applied_false_when_nothing_is_json() -> None:
    from mcp_broker.catalog import apply_projection

    response = {"content": [{"type": "text", "text": "plain text"}]}

    projected = apply_projection(response, {"max_array_items": 1})

    assert projected["content"][0]["text"] == "plain text"
    assert projected["_meta"]["projection"]["applied"] is False

def test_project_value_cap_zero_empties_arrays() -> None:
    from mcp_broker.catalog import project_value

    # cap=0 is a valid non-negative cap and must truncate to empty, not be ignored.
    assert project_value({"rows": [{"a": 1}, {"a": 2}]}, None, 0) == {"rows": []}

def test_project_value_cap_keeps_prefix_not_suffix() -> None:
    from mcp_broker.catalog import project_value

    # Distinguishes projected[:cap] from projected[cap:] / projected[-cap:].
    assert project_value([10, 20, 30, 40], None, 2) == [10, 20]

def test_project_value_maps_over_top_level_list_payload() -> None:
    from mcp_broker.catalog import project_value

    payload = [{"id": 1, "x": "a"}, {"id": 2, "x": "b"}]
    assert project_value(payload, ["id"], None) == [{"id": 1}, {"id": 2}]

def test_project_value_empty_tree_keeps_all_keys_but_still_caps() -> None:
    from mcp_broker.catalog import project_value

    # A fully consumed path ("item") keeps the whole subtree, and the cap still
    # applies to lists inside it.
    payload = {"item": {"id": 1, "tags": ["a", "b", "c"]}}
    assert project_value(payload, ["item"], 1) == {"item": {"id": 1, "tags": ["a"]}}

def test_normalize_projection_accepts_cap_zero() -> None:
    from mcp_broker.catalog import apply_projection

    # cap=0 must be accepted (boundary: cap < 0 rejects, cap == 0 allowed).
    out = apply_projection(
        {"structuredContent": {"rows": [1, 2, 3]}, "content": []},
        {"max_array_items": 0},
    )
    assert out["structuredContent"] == {"rows": []}
    assert out["_meta"]["projection"]["max_array_items"] == 0

def test_normalize_projection_filters_empty_path_strings() -> None:
    from mcp_broker.catalog import apply_projection

    # An empty path string is dropped; the remaining path still selects.
    out = apply_projection(
        {"structuredContent": {"id": 1, "drop": 2}, "content": []},
        {"paths": ["", "id"]},
    )
    assert out["structuredContent"] == {"id": 1}

def test_normalize_projection_empty_paths_list_is_noop() -> None:
    from mcp_broker.catalog import apply_projection

    # paths=[] normalizes to None; with no cap that is a no-op (response returned as-is).
    response = {"structuredContent": {"id": 1, "keep": 2}, "content": []}
    assert apply_projection(response, {"paths": []}) == response

def test_apply_projection_handles_list_structured_content() -> None:
    from mcp_broker.catalog import apply_projection

    response = {"structuredContent": [{"id": 1, "x": "a"}], "content": []}
    out = apply_projection(response, {"paths": ["id"]})
    assert out["structuredContent"] == [{"id": 1}]
    assert out["_meta"]["projection"]["applied"] is True

def test_apply_projection_cap_only_truncates_and_marks_applied() -> None:
    from mcp_broker.catalog import apply_projection

    response = {"structuredContent": {"rows": [1, 2, 3, 4]}, "content": []}
    out = apply_projection(response, {"max_array_items": 2})
    assert out["structuredContent"] == {"rows": [1, 2]}
    assert out["_meta"]["projection"]["applied"] is True
    assert out["_meta"]["projection"]["paths"] == []

def test_project_text_block_ignores_block_whose_text_is_not_a_string() -> None:
    from mcp_broker.catalog import apply_projection

    # type == "text" but text is a number: must be left untouched (not pruned).
    response = {"content": [{"type": "text", "text": 123}]}
    out = apply_projection(response, {"paths": ["id"]})
    assert out["content"][0] == {"type": "text", "text": 123}
    assert out["_meta"]["projection"]["applied"] is False

def test_project_text_block_ignores_non_text_typed_block_with_text_field() -> None:
    from mcp_broker.catalog import apply_projection

    # A non-"text" type carrying a JSON-looking text field must NOT be pruned -
    # isolates the type=="text" gate from the text-is-str gate.
    response = {"content": [{"type": "resource", "text": json.dumps({"id": 1, "drop": 2})}]}
    out = apply_projection(response, {"paths": ["id"]})
    assert out["content"][0]["text"] == json.dumps({"id": 1, "drop": 2})
    assert out["_meta"]["projection"]["applied"] is False

def test_apply_projection_passes_through_non_text_content_blocks() -> None:
    from mcp_broker.catalog import apply_projection

    response = {"content": [{"type": "image", "data": "base64..."}]}

    projected = apply_projection(response, {"max_array_items": 1})

    assert projected["content"][0] == {"type": "image", "data": "base64..."}
    assert projected["_meta"]["projection"]["applied"] is False

def test_apply_projection_rejects_non_object_projection() -> None:
    # Exact message (not just the exception type) so message-string mutations die.
    assert _projection_error_message("not-an-object") == "projection must be an object"
