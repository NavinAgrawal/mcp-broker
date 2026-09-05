from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_broker import governance_reference_control_plane as control_plane


pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]


@pytest.mark.parametrize(
    "error",
    [
        control_plane.GovernanceReferenceControlPlaneError("governance error"),
        OSError("filesystem error"),
        json.JSONDecodeError("json error", "{", 0),
    ],
)
def test_reference_main_translates_supported_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    parser = SimpleNamespace(
        parse_args=lambda argv: SimpleNamespace(
            mode="local_reference_only",
            state_dir=Path("state"),
            bundle=Path("bundle.json"),
            assignment_source=Path("assignment.json"),
            broker_context=Path("context.json"),
            fleet_status=Path("fleet.json"),
            target_url="https://control.example.invalid/status",
            auth_ref="env:TOKEN",
            operator="operator-1",
            signature_ref="sigstore:bundle.sig",
            provenance=Path("provenance.json"),
            approval_expires_at="2026-07-04T07:30:00Z",
            created_at=None,
        )
    )
    monkeypatch.setattr(control_plane, "_parser", lambda: parser)
    monkeypatch.setattr(
        control_plane,
        "_load_json_mapping",
        lambda path: (_ for _ in ()).throw(error),
    )

    assert control_plane.main([]) == 1
    assert capsys.readouterr().out == f"{error}\n"
