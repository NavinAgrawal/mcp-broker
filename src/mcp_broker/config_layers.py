"""Deterministic layered config composition."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

import yaml


_SECRET_KEY_PATTERN = re.compile(r"(secret|token|credential|password|api[_-]?key|key)", re.I)
_SECRET_REF_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigLayerError(ValueError):
    """Raised when layered config composition fails."""


@dataclass(frozen=True)
class LayerDocument:
    name: str
    source: Path
    data: dict[str, Any]


@dataclass(frozen=True)
class LayeredConfigResult:
    effective_config: dict[str, Any]
    digest: str
    layers: list[str]
    provenance: dict[str, dict[str, str]]
    conflicts: list[dict[str, str]]

    def as_summary(self) -> dict[str, Any]:
        return {
            "changed_runtime_state": False,
            "effective_config_digest": self.digest,
            "layers": self.layers,
            "effective_config": self.effective_config,
            "provenance": self.provenance,
            "conflicts": self.conflicts,
        }


def compose_layered_config(
    *,
    org: LayerDocument | None = None,
    team: LayerDocument | None = None,
    add_ons: Sequence[LayerDocument] = (),
    user: LayerDocument | None = None,
) -> LayeredConfigResult:
    layers = [layer for layer in (org, team, *add_ons, user) if layer is not None]
    if not layers:
        raise ConfigLayerError("at least one config layer is required")

    effective: dict[str, Any] = {}
    provenance: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, str]] = []

    for layer in layers:
        _validate_layer(layer)
        _merge_mapping(
            effective,
            copy.deepcopy(layer.data),
            layer=layer,
            provenance=provenance,
            conflicts=conflicts,
            path=(),
        )

    return LayeredConfigResult(
        effective_config=effective,
        digest=f"sha256:{_digest(effective)}",
        layers=[layer.name for layer in layers],
        provenance=dict(sorted(provenance.items())),
        conflicts=conflicts,
    )


def load_layer_document(path: Path, name: str | None = None) -> LayerDocument:
    resolved = path.expanduser()
    if not resolved.exists():
        raise ConfigLayerError(f"config layer file not found: {resolved}")
    if not resolved.is_file():
        raise ConfigLayerError(f"config layer path must be a file: {resolved}")

    with resolved.open("r", encoding="utf-8") as handle:
        try:
            if resolved.suffix.lower() == ".json":
                loaded = json.load(handle)
            else:
                loaded = yaml.safe_load(handle)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ConfigLayerError(f"config layer file is invalid: {resolved}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigLayerError(f"config layer must contain an object: {resolved}")
    return LayerDocument(name=name or resolved.stem, source=resolved, data=loaded)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        add_ons = [
            load_layer_document(add_on)
            for add_on in args.addon
        ]
        result = compose_layered_config(
            org=_optional_layer(args.org, "org"),
            team=_optional_layer(args.team, "team"),
            add_ons=add_ons,
            user=_optional_layer(args.user, "user"),
        )
    except ConfigLayerError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    sys.stdout.write(json.dumps(result.as_summary(), indent=2, sort_keys=True))
    sys.stdout.write("\n")
    return 0


def _optional_layer(path: Path | None, name: str) -> LayerDocument | None:
    if path is None:
        return None
    return load_layer_document(path, name=name)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compose layered mcp-broker config")
    parser.add_argument("--org", type=Path)
    parser.add_argument("--team", type=Path)
    parser.add_argument("--addon", action="append", default=[], type=Path)
    parser.add_argument("--user", type=Path)
    return parser.parse_args(argv)


def _merge_mapping(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    layer: LayerDocument,
    provenance: dict[str, dict[str, str]],
    conflicts: list[dict[str, str]],
    path: tuple[str, ...],
) -> None:
    for key in sorted(incoming):
        child_path = (*path, str(key))
        dotted = ".".join(child_path)
        incoming_value = incoming[key]
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(incoming_value, dict)
            and not _is_secret_ref(incoming_value)
        ):
            _merge_mapping(
                target[key],
                incoming_value,
                layer=layer,
                provenance=provenance,
                conflicts=conflicts,
                path=child_path,
            )
            continue

        if key in target and target[key] != incoming_value:
            conflicts.append(
                {
                    "path": dotted,
                    "previous_layer": _previous_layer_for_path(provenance, dotted),
                    "new_layer": layer.name,
                }
            )
            _clear_provenance(provenance, dotted)
        target[key] = incoming_value
        _record_provenance(incoming_value, layer, provenance, child_path)


def _record_provenance(
    value: Any,
    layer: LayerDocument,
    provenance: dict[str, dict[str, str]],
    path: tuple[str, ...],
) -> None:
    dotted = ".".join(path)
    if isinstance(value, dict) and not _is_secret_ref(value):
        for key in sorted(value):
            _record_provenance(value[key], layer, provenance, (*path, str(key)))
        return
    provenance[dotted] = {"layer": layer.name, "source": str(layer.source)}


def _previous_layer_for_path(provenance: dict[str, dict[str, str]], dotted: str) -> str:
    exact = provenance.get(dotted)
    if exact is not None:
        return exact["layer"]
    prefix = f"{dotted}."
    layers = {
        entry["layer"]
        for path, entry in provenance.items()
        if path.startswith(prefix)
    }
    if len(layers) == 1:
        return next(iter(layers))
    if layers:
        return "mixed"
    raise ConfigLayerError(f"missing provenance for conflict path: {dotted}")


def _clear_provenance(provenance: dict[str, dict[str, str]], dotted: str) -> None:
    prefix = f"{dotted}."
    for path in list(provenance):
        if path == dotted or path.startswith(prefix):
            del provenance[path]


def _validate_layer(layer: LayerDocument) -> None:
    if not layer.name.strip():
        raise ConfigLayerError("config layer name is required")
    if not isinstance(layer.data, dict):
        raise ConfigLayerError(f"config layer must contain an object: {layer.name}")
    _validate_secret_references(layer.data, layer=layer.name, path=())


def _validate_secret_references(value: Any, *, layer: str, path: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        if "secret_value" in value:
            raise ConfigLayerError(
                f"literal secret value is not allowed in layer {layer} at {'.'.join(path)}"
            )
        if "secret_ref" in value:
            _validate_secret_ref(value["secret_ref"], layer=layer, path=path)
            return
        for key, child in value.items():
            child_path = (*path, str(key))
            if _SECRET_KEY_PATTERN.search(str(key)) and isinstance(child, str):
                raise ConfigLayerError(
                    "literal secret value is not allowed in layer "
                    f"{layer} at {'.'.join(child_path)}"
                )
            _validate_secret_references(child, layer=layer, path=child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_secret_references(child, layer=layer, path=(*path, str(index)))


def _validate_secret_ref(value: Any, *, layer: str, path: tuple[str, ...]) -> None:
    if not isinstance(value, str) or not _SECRET_REF_PATTERN.fullmatch(value):
        raise ConfigLayerError(
            f"secret_ref must name an environment variable in layer {layer} at {'.'.join(path)}"
        )


def _is_secret_ref(value: dict[str, Any]) -> bool:
    return set(value) == {"secret_ref"}


def _digest(effective_config: dict[str, Any]) -> str:
    payload = json.dumps(effective_config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
