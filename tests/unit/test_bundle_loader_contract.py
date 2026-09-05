import json
from pathlib import Path
import runpy
import sys

import pytest

from tests.support.bundles import minimal_bundle, signed_bundle, write_signed_bundle


pytestmark = pytest.mark.unit


def test_validate_bundle_file_accepts_schema_checksum_and_compatibility(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import validate_bundle_file

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    report = validate_bundle_file(bundle_path)

    assert report == {
        "bundle_path": str(bundle_path),
        "bundle_id": "personal-local",
        "version": "2026.07.01",
        "schema_version": 1,
        "checksum_algorithm": "sha256",
        "checksum_verified": True,
        "compatible": True,
        "changed_runtime_state": False,
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.json"]


def test_validate_bundle_file_rejects_missing_file(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    with pytest.raises(BundleValidationError, match="bundle file not found"):
        validate_bundle_file(tmp_path / "missing.json")


def test_validate_bundle_file_rejects_directory_path(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    with pytest.raises(BundleValidationError, match="bundle path must be a file"):
        validate_bundle_file(tmp_path)


def test_validate_bundle_file_rejects_invalid_json(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{", encoding="utf-8")

    with pytest.raises(BundleValidationError, match="bundle file must contain valid JSON"):
        validate_bundle_file(bundle_path)


def test_validate_bundle_file_rejects_non_object_json(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("[]", encoding="utf-8")

    with pytest.raises(BundleValidationError, match="bundle file must contain a JSON object"):
        validate_bundle_file(bundle_path)


def test_validate_bundle_file_rejects_checksum_mismatch(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle = signed_bundle()
    bundle["checksum"]["value"] = "f" * 64
    bundle_path = _write_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(BundleValidationError, match="checksum mismatch"):
        validate_bundle_file(bundle_path)


def test_validate_bundle_file_rejects_schema_errors_before_loader(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle = signed_bundle()
    bundle["install_script"] = "./setup.sh"
    bundle_path = _write_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(BundleValidationError, match="schema validation failed"):
        validate_bundle_file(bundle_path)


def test_validate_bundle_file_rejects_wrong_schema_version(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle = minimal_bundle()
    bundle["schema_version"] = 2
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(BundleValidationError, match="schema validation failed"):
        validate_bundle_file(bundle_path)


def test_defensive_schema_version_guard_rejects_drift() -> None:
    from mcp_broker.bundle_loader import BundleValidationError, _validate_schema_version

    with pytest.raises(BundleValidationError, match="unsupported bundle schema version"):
        _validate_schema_version({"schema_version": 2})


def test_validate_bundle_file_rejects_incompatible_config_schema(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle = minimal_bundle()
    bundle["compatibility"]["min_config_schema_version"] = 2
    bundle["compatibility"]["max_config_schema_version"] = 2
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(BundleValidationError, match="incompatible config schema version"):
        validate_bundle_file(bundle_path)


def test_validate_bundle_file_rejects_compatibility_max_below_broker(tmp_path: Path) -> None:
    from mcp_broker.bundle_loader import BundleValidationError, validate_bundle_file

    bundle = minimal_bundle()
    bundle["compatibility"]["min_config_schema_version"] = 0
    bundle["compatibility"]["max_config_schema_version"] = 0
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(BundleValidationError, match="schema validation failed"):
        validate_bundle_file(bundle_path)


def test_defensive_compatibility_guard_rejects_max_below_broker() -> None:
    from mcp_broker.bundle_loader import BundleValidationError, _validate_compatibility

    with pytest.raises(BundleValidationError, match="incompatible config schema version"):
        _validate_compatibility(
            {
                "compatibility": {
                    "min_config_schema_version": 1,
                    "max_config_schema_version": 0,
                }
            }
        )


def test_bundle_loader_cli_reports_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.bundle_loader import main

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    assert main(["--bundle", str(bundle_path)]) == 0
    captured = capsys.readouterr()
    assert "bundle validated:" in captured.out
    assert "personal-local 2026.07.01" in captured.out
    assert captured.err == ""


def test_bundle_loader_cli_reports_validation_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.bundle_loader import main

    assert main(["--bundle", str(tmp_path / "missing.json")]) == 1
    captured = capsys.readouterr()
    assert "bundle file not found" in captured.out
    assert captured.err == ""


@pytest.mark.error_simulation
def test_bundle_loader_module_entrypoint_exits_with_main_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = write_signed_bundle(tmp_path / "bundle.json")
    monkeypatch.setattr(sys, "argv", ["bundle_loader", "--bundle", str(bundle_path)])

    module_name = "mcp_broker.bundle_loader"
    previous_module = sys.modules.pop(module_name, None)

    try:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module(module_name, run_name="__main__")
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

    assert exit_info.value.code == 0


def _write_bundle(path: Path, bundle: dict[str, object]) -> Path:
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    return path
