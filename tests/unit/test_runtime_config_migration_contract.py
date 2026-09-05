from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_migration_module() -> object:
    script = ROOT / "scripts" / "migrate-runtime-config.py"
    assert script.is_file()
    spec = importlib.util.spec_from_file_location("runtime_config_migration", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migration_plan_rejects_divergent_destination(tmp_path: Path) -> None:
    module = _load_migration_module()
    source = tmp_path / "public" / "config" / "broker.private.yaml"
    destination = tmp_path / "runtime" / "config" / "broker.private.yaml"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_text("runtime: source\n", encoding="utf-8")
    destination.write_text("runtime: destination\n", encoding="utf-8")

    plan = module.plan_migration(source=source, destination=destination)

    assert plan.status == "divergent_destination"
    assert not plan.can_apply
    assert plan.backup_path is None


def test_migration_plan_accepts_missing_destination(tmp_path: Path) -> None:
    module = _load_migration_module()
    source = tmp_path / "public" / "config" / "broker.private.yaml"
    destination = tmp_path / "runtime" / "config" / "broker.private.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("runtime: source\n", encoding="utf-8")

    plan = module.plan_migration(source=source, destination=destination)

    assert plan.status == "ready"
    assert plan.can_apply
    assert plan.backup_path is None
