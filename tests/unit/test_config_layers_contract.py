import json
from pathlib import Path
import runpy
import sys

import pytest


pytestmark = pytest.mark.unit


def test_compose_layered_config_merges_in_fixed_order_and_reports_digest() -> None:
    from mcp_broker.config_layers import LayerDocument, compose_layered_config

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={
            "clients": {"codex": {"command": "mcp-broker-client"}},
            "profiles": {"codex": {"tools": ["broker_search_tools"]}},
            "upstreams": {"github": {"enabled": False, "call_timeout_seconds": 30}},
        },
    )
    team = LayerDocument(
        name="team",
        source=Path("team.yaml"),
        data={
            "clients": {"codex": {"command": "team-mcp-broker-client"}},
            "upstreams": {"github": {"enabled": True}},
        },
    )
    add_on = LayerDocument(
        name="observability",
        source=Path("observability.yaml"),
        data={
            "policy": {"audit": {"enabled": True}},
            "upstreams": {"github": {"call_timeout_seconds": 20}},
        },
    )
    user = LayerDocument(
        name="user",
        source=Path("user.yaml"),
        data={"upstreams": {"github": {"call_timeout_seconds": 10}}},
    )

    result = compose_layered_config(org=org, team=team, add_ons=[add_on], user=user)

    assert result.effective_config == {
        "clients": {"codex": {"command": "team-mcp-broker-client"}},
        "policy": {"audit": {"enabled": True}},
        "profiles": {"codex": {"tools": ["broker_search_tools"]}},
        "upstreams": {"github": {"enabled": True, "call_timeout_seconds": 10}},
    }
    assert result.digest.startswith("sha256:")
    assert result.layers == ["org", "team", "observability", "user"]
    assert result.provenance == {
        "clients.codex.command": {"layer": "team", "source": "team.yaml"},
        "policy.audit.enabled": {
            "layer": "observability",
            "source": "observability.yaml",
        },
        "profiles.codex.tools": {"layer": "org", "source": "org.yaml"},
        "upstreams.github.call_timeout_seconds": {
            "layer": "user",
            "source": "user.yaml",
        },
        "upstreams.github.enabled": {"layer": "team", "source": "team.yaml"},
    }
    assert result.conflicts == [
        {
            "path": "clients.codex.command",
            "previous_layer": "org",
            "new_layer": "team",
        },
        {
            "path": "upstreams.github.enabled",
            "previous_layer": "org",
            "new_layer": "team",
        },
        {
            "path": "upstreams.github.call_timeout_seconds",
            "previous_layer": "org",
            "new_layer": "observability",
        },
        {
            "path": "upstreams.github.call_timeout_seconds",
            "previous_layer": "observability",
            "new_layer": "user",
        },
    ]
    assert result.as_summary()["changed_runtime_state"] is False


def test_compose_layered_config_rejects_literal_secret_values() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={
            "upstreams": {
                "github": {
                    "env": {
                        "GITHUB_TOKEN": {"secret_ref": "GITHUB_TOKEN"},
                        "BAD_API_KEY": "plain-secret-value",
                    }
                }
            }
        },
    )

    with pytest.raises(ConfigLayerError, match="literal secret value"):
        compose_layered_config(org=org)


def test_compose_layered_config_rejects_invalid_secret_ref_names() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={"upstreams": {"github": {"env": {"TOKEN": {"secret_ref": "token-value"}}}}},
    )

    with pytest.raises(ConfigLayerError) as exc_info:
        compose_layered_config(org=org)
    assert str(exc_info.value) == (
        "secret_ref must name an environment variable in layer org at "
        "upstreams.github.env.TOKEN"
    )


def test_compose_layered_config_requires_at_least_one_layer() -> None:
    from mcp_broker.config_layers import ConfigLayerError, compose_layered_config

    with pytest.raises(ConfigLayerError) as exc_info:
        compose_layered_config()
    assert str(exc_info.value) == "at least one config layer is required"


def test_compose_layered_config_does_not_mutate_input_layers() -> None:
    from mcp_broker.config_layers import LayerDocument, compose_layered_config

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={"profiles": {"codex": {"tools": ["broker_status"]}}},
    )
    team = LayerDocument(
        name="team",
        source=Path("team.yaml"),
        data={"profiles": {"codex": {"enabled": True}}},
    )

    compose_layered_config(org=org, team=team)

    assert org.data == {"profiles": {"codex": {"tools": ["broker_status"]}}}
    assert team.data == {"profiles": {"codex": {"enabled": True}}}


def test_compose_layered_config_digest_covers_effective_config() -> None:
    from mcp_broker.config_layers import LayerDocument, compose_layered_config

    result = compose_layered_config(
        org=LayerDocument(
            name="org",
            source=Path("org.yaml"),
            data={"b": 2, "a": 1},
        )
    )

    assert result.digest == (
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )


def test_compose_layered_config_handles_parent_replacements() -> None:
    from mcp_broker.config_layers import LayerDocument, compose_layered_config

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={"upstreams": {"github": {"enabled": True}}},
    )
    team = LayerDocument(
        name="team",
        source=Path("team.yaml"),
        data={"upstreams": {"github": "disabled-by-policy"}},
    )
    user = LayerDocument(
        name="user",
        source=Path("user.yaml"),
        data={"upstreams": {"github": {"enabled": False}}},
    )

    result = compose_layered_config(org=org, team=team, user=user)

    assert result.effective_config == {"upstreams": {"github": {"enabled": False}}}
    assert result.conflicts == [
        {
            "path": "upstreams.github",
            "previous_layer": "org",
            "new_layer": "team",
        },
        {
            "path": "upstreams.github",
            "previous_layer": "team",
            "new_layer": "user",
        },
    ]
    assert result.provenance == {
        "upstreams.github.enabled": {"layer": "user", "source": "user.yaml"}
    }


def test_compose_layered_config_rejects_blank_layer_name() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    with pytest.raises(ConfigLayerError) as exc_info:
        compose_layered_config(org=LayerDocument(name="", source=Path("org.yaml"), data={}))
    assert str(exc_info.value) == "config layer name is required"


def test_compose_layered_config_rejects_non_mapping_layer_data() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    with pytest.raises(ConfigLayerError, match="must contain an object"):
        compose_layered_config(
            org=LayerDocument(name="org", source=Path("org.yaml"), data=[]),
        )


def test_compose_layered_config_rejects_literal_secret_value_key() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    with pytest.raises(ConfigLayerError, match="literal secret value"):
        compose_layered_config(
            org=LayerDocument(
                name="org",
                source=Path("org.yaml"),
                data={"upstreams": {"github": {"secret_value": "plain"}}},
            )
        )


def test_compose_layered_config_validates_secret_refs_inside_lists() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        LayerDocument,
        compose_layered_config,
    )

    with pytest.raises(ConfigLayerError, match="secret_ref must name"):
        compose_layered_config(
            org=LayerDocument(
                name="org",
                source=Path("org.yaml"),
                data={"upstreams": [{"env": {"TOKEN": {"secret_ref": "bad-token"}}}]},
            )
        )


def test_compose_layered_config_reports_mixed_previous_layer_for_parent_conflict() -> None:
    from mcp_broker.config_layers import LayerDocument, compose_layered_config

    org = LayerDocument(
        name="org",
        source=Path("org.yaml"),
        data={"upstreams": {"github": {"enabled": True}}},
    )
    team = LayerDocument(
        name="team",
        source=Path("team.yaml"),
        data={"upstreams": {"github": {"call_timeout_seconds": 30}}},
    )
    user = LayerDocument(
        name="user",
        source=Path("user.yaml"),
        data={"upstreams": {"github": "disabled"}},
    )

    result = compose_layered_config(org=org, team=team, user=user)

    assert result.conflicts[-1] == {
        "path": "upstreams.github",
        "previous_layer": "mixed",
        "new_layer": "user",
    }


def test_load_layer_document_reads_json_and_yaml(tmp_path: Path) -> None:
    from mcp_broker.config_layers import load_layer_document

    json_path = tmp_path / "org.json"
    yaml_path = tmp_path / "team.yaml"
    json_path.write_text(json.dumps({"profiles": {"codex": {}}}), encoding="utf-8")
    yaml_path.write_text("profiles:\n  codex: {}\n", encoding="utf-8")

    json_layer = load_layer_document(json_path)
    yaml_layer = load_layer_document(yaml_path, name="team-layer")

    assert json_layer.name == "org"
    assert json_layer.source == json_path
    assert json_layer.data == {"profiles": {"codex": {}}}
    assert yaml_layer.name == "team-layer"
    assert yaml_layer.source == yaml_path
    assert yaml_layer.data == {"profiles": {"codex": {}}}


def test_load_layer_document_enforces_json_syntax_for_json_suffix(
    tmp_path: Path,
) -> None:
    from mcp_broker.config_layers import ConfigLayerError, load_layer_document

    json_path = tmp_path / "org.JSON"
    json_path.write_text("profiles:\n  codex: {}\n", encoding="utf-8")

    with pytest.raises(ConfigLayerError, match="config layer file is invalid"):
        load_layer_document(json_path)


def test_load_layer_document_opens_config_as_explicit_utf8_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config_layers import load_layer_document

    path = tmp_path / "org.yaml"
    path.write_text("display_name: Caf\u00e9\n", encoding="utf-8")
    original_open = Path.open
    open_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def recording_open(
        self: Path,
        *args: object,
        **kwargs: object,
    ):
        open_calls.append((args, kwargs))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    layer = load_layer_document(path)

    assert layer.data == {"display_name": "Caf\u00e9"}
    assert open_calls == [(('r',), {"encoding": "utf-8"})]


def test_load_layer_document_rejects_missing_directory_invalid_and_non_object(
    tmp_path: Path,
) -> None:
    from mcp_broker.config_layers import ConfigLayerError, load_layer_document

    with pytest.raises(ConfigLayerError, match="not found"):
        load_layer_document(tmp_path / "missing.yaml")

    with pytest.raises(ConfigLayerError, match="must be a file"):
        load_layer_document(tmp_path)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("profiles: [", encoding="utf-8")
    with pytest.raises(ConfigLayerError, match="invalid"):
        load_layer_document(invalid)

    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigLayerError, match="must contain an object"):
        load_layer_document(non_object)


def test_config_layers_cli_reports_summary_and_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_layers import main

    org = tmp_path / "org.yaml"
    org.write_text("profiles:\n  codex:\n    tools:\n      - broker_status\n", encoding="utf-8")

    assert main(["--org", str(org)]) == 0
    assert '"changed_runtime_state": false' in capsys.readouterr().out

    assert main(["--org", str(tmp_path / "missing.yaml")]) == 1
    assert "config layer file not found" in capsys.readouterr().err


def test_config_layers_main_preserves_role_names_and_exact_json_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_layers import main

    paths = {
        "org": tmp_path / "enterprise.yaml",
        "team": tmp_path / "squad.yaml",
        "addon": tmp_path / "feature.yaml",
        "user": tmp_path / "person.yaml",
    }
    for index, path in enumerate(paths.values()):
        path.write_text(f"setting_{index}: {index}\n", encoding="utf-8")

    assert main(
        [
            "--org",
            str(paths["org"]),
            "--team",
            str(paths["team"]),
            "--addon",
            str(paths["addon"]),
            "--user",
            str(paths["user"]),
        ]
    ) == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["layers"] == ["org", "team", "feature", "user"]
    assert stdout == json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_config_layers_optional_layer_forwards_explicit_role_name(
    tmp_path: Path,
) -> None:
    from mcp_broker.config_layers import _optional_layer

    path = tmp_path / "different-stem.yaml"
    path.write_text("setting: true\n", encoding="utf-8")

    layer = _optional_layer(path, "org")
    assert layer is not None
    assert layer.name == "org"
    assert _optional_layer(None, "org") is None


def test_config_layers_parser_has_exact_public_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.config_layers import _parse_args

    parsed = _parse_args(
        [
            "--org",
            "org.yaml",
            "--team",
            "team.yaml",
            "--addon",
            "first.yaml",
            "--addon",
            "second.yaml",
            "--user",
            "user.yaml",
        ]
    )
    assert vars(parsed) == {
        "org": Path("org.yaml"),
        "team": Path("team.yaml"),
        "addon": [Path("first.yaml"), Path("second.yaml")],
        "user": Path("user.yaml"),
    }
    with pytest.raises(SystemExit) as exit_info:
        _parse_args(["--help"])
    assert exit_info.value.code == 0
    assert "Compose layered mcp-broker config" in capsys.readouterr().out.splitlines()


def test_config_layers_defensive_previous_layer_guard_requires_provenance() -> None:
    from mcp_broker.config_layers import ConfigLayerError, _previous_layer_for_path

    with pytest.raises(ConfigLayerError, match="missing provenance"):
        _previous_layer_for_path({}, "upstreams.github")


def test_config_layers_secret_ref_helpers_enforce_exact_contract() -> None:
    from mcp_broker.config_layers import (
        ConfigLayerError,
        _is_secret_ref,
        _validate_secret_ref,
    )

    assert _is_secret_ref({"secret_ref": "TOKEN"}) is True
    assert _is_secret_ref({"SECRET_REF": "TOKEN"}) is False
    assert _is_secret_ref({"secret_ref": "TOKEN", "extra": True}) is False
    assert _validate_secret_ref("CONTROL_TOKEN", layer="org", path=("env", "TOKEN")) is None

    for invalid in ("token-value", 7):
        with pytest.raises(ConfigLayerError) as exc_info:
            _validate_secret_ref(invalid, layer="org", path=("env", "TOKEN"))
        assert str(exc_info.value) == (
            "secret_ref must name an environment variable in layer org at env.TOKEN"
        )


def test_config_layers_secret_scanner_reports_exact_nested_paths() -> None:
    from mcp_broker.config_layers import ConfigLayerError, _validate_secret_references

    cases = [
        (
            {"nested": {"secret_value": "plain"}},
            "literal secret value is not allowed in layer team at root.nested",
        ),
        (
            {"nested": {"API_KEY": "plain"}},
            "literal secret value is not allowed in layer team at root.nested.API_KEY",
        ),
        (
            [{"secret_ref": "bad-token"}],
            "secret_ref must name an environment variable in layer team at root.0",
        ),
    ]
    for value, message in cases:
        with pytest.raises(ConfigLayerError) as exc_info:
            _validate_secret_references(value, layer="team", path=("root",))
        assert str(exc_info.value) == message


def test_config_layers_digest_is_canonical_sha256() -> None:
    from mcp_broker.config_layers import _digest

    assert _digest({"b": 2, "a": 1}) == (
        "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )


@pytest.mark.error_simulation
def test_config_layers_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = tmp_path / "org.yaml"
    org.write_text("profiles:\n  codex:\n    tools:\n      - broker_status\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["config_layers", "--org", str(org)])

    module_name = "mcp_broker.config_layers"
    previous_module = sys.modules.pop(module_name, None)
    try:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module("mcp_broker.config_layers", run_name="__main__")
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert exit_info.value.code == 0
