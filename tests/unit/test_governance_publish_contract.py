import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from tests.support.bundles import minimal_bundle, signed_bundle, write_signed_bundle


pytestmark = pytest.mark.unit


PUBLISH_PROVENANCE = {
    "repository": "mcp-broker",
    "commit": "abc1234",
    "builder": "local-publisher",
}
SIGNATURE_REF = "sigstore:governance-bundle.sig"


def test_bundle_schema_accepts_signed_publish_metadata() -> None:
    from mcp_broker.bundle_schema import load_bundle_schema

    bundle = signed_bundle()
    bundle["publish"] = {
        "signature_ref": SIGNATURE_REF,
        "promotion_state": "candidate",
        "revoked": False,
        "provenance": PUBLISH_PROVENANCE,
    }

    Draft202012Validator(load_bundle_schema()).validate(bundle)


def test_bundle_schema_rejects_publish_metadata_without_signature() -> None:
    from mcp_broker.bundle_schema import load_bundle_schema

    bundle = signed_bundle()
    bundle["publish"] = {
        "promotion_state": "candidate",
        "revoked": False,
        "provenance": PUBLISH_PROVENANCE,
    }

    with pytest.raises(ValidationError, match="'signature_ref' is a required property"):
        Draft202012Validator(load_bundle_schema()).validate(bundle)


def test_publish_bundle_writes_signed_manifest_without_runtime_mutation(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import publish_bundle

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")
    manifest_path = publish_bundle(
        bundle_path=bundle_path,
        output_dir=tmp_path / "published",
        signature_ref=SIGNATURE_REF,
        provenance=PUBLISH_PROVENANCE,
        promotion_state="candidate",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest == {
        "schema_version": 1,
        "bundle": {
            "bundle_id": "personal-local",
            "version": "2026.07.01",
            "channel": "stable",
            "digest": {
                "algorithm": "sha256",
                "value": signed_bundle()["checksum"]["value"],
            },
            "compatibility": {
                "min_config_schema_version": 1,
                "max_config_schema_version": 1,
                "required_features": ["broker.identity"],
            },
        },
        "signature": {
            "required": True,
            "signature_ref": SIGNATURE_REF,
        },
        "promotion": {
            "state": "candidate",
            "channel": "stable",
        },
        "revocation": {
            "revoked": False,
            "reason": "",
        },
        "provenance": PUBLISH_PROVENANCE,
        "changed_runtime_state": False,
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.json", "published"]


def test_publish_bundle_rejects_unsigned_candidates(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    with pytest.raises(GovernancePublishError, match="signature_ref is required"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref="",
            provenance=PUBLISH_PROVENANCE,
            promotion_state="candidate",
        )


def test_publish_bundle_rejects_unchecksummed_candidates(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle = minimal_bundle()
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(GovernancePublishError, match="checksum mismatch"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref=SIGNATURE_REF,
            provenance=PUBLISH_PROVENANCE,
            promotion_state="candidate",
        )


def test_publish_bundle_rejects_revoked_candidates(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    with pytest.raises(GovernancePublishError, match="revoked publish candidates cannot be promoted"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref=SIGNATURE_REF,
            provenance=PUBLISH_PROVENANCE,
            promotion_state="candidate",
            revoked=True,
            revocation_reason="operator hold",
        )


def test_publish_bundle_rejects_unsafe_manifest_name_fields(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle = minimal_bundle()
    bundle["version"] = "../../escape"
    bundle_path = write_signed_bundle(tmp_path / "bundle.json", bundle)

    with pytest.raises(GovernancePublishError, match="unsafe publish manifest field: version"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref=SIGNATURE_REF,
            provenance=PUBLISH_PROVENANCE,
            promotion_state="candidate",
        )


def test_publish_bundle_rejects_unknown_promotion_state(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    with pytest.raises(GovernancePublishError, match="unsupported promotion state"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref=SIGNATURE_REF,
            provenance=PUBLISH_PROVENANCE,
            promotion_state="preview",
        )


def test_publish_bundle_rejects_incomplete_provenance(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, publish_bundle

    bundle_path = write_signed_bundle(tmp_path / "bundle.json")

    with pytest.raises(GovernancePublishError, match="missing publish provenance field: commit"):
        publish_bundle(
            bundle_path=bundle_path,
            output_dir=tmp_path / "published",
            signature_ref=SIGNATURE_REF,
            provenance={"repository": "mcp-broker", "builder": "local-publisher"},
            promotion_state="candidate",
        )


def test_defensive_publish_bundle_loader_rejects_non_object_json(tmp_path: Path) -> None:
    from mcp_broker.governance_publish import GovernancePublishError, _load_bundle

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("[]", encoding="utf-8")

    with pytest.raises(GovernancePublishError, match="bundle file must contain a JSON object"):
        _load_bundle(bundle_path)
