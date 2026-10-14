#!/usr/bin/env python3
"""Fail closed unless every generation-9 App identity consumer agrees."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


_PACKAGING_ROOT = Path(__file__).resolve().parent
_APP_ROOT = _PACKAGING_ROOT.parent
for _root in (_PACKAGING_ROOT, _APP_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from phase10_finalize_candidate import _tree_digest as phase10_tree_digest
from src.core.agent_entry.canonical_app_tree import canonical_app_tree_identity
from src.core.agent_entry.component_manifest import verify_component_manifest


SCHEMA_ID = "lulu.generation9-candidate-identity-receipt/v1"


class CandidateIdentityError(RuntimeError):
    """The finalized candidate has identity drift between consumers."""


def _json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateIdentityError("CANDIDATE_IDENTITY_RECEIPT_UNREADABLE") from exc
    if type(payload) is not dict:
        raise CandidateIdentityError("CANDIDATE_IDENTITY_RECEIPT_INVALID")
    return payload


def verify_candidate_identity(
    *, app: Path, profile_receipt_path: Path
) -> dict[str, object]:
    app = app.resolve(strict=True)
    if (app/"Contents/Resources/component_delivery.v2.json").exists():
        from src.core.agent_entry.delivery_identity import delivery_app_tree_identity, MANIFEST_NAME
        first=delivery_app_tree_identity(app);second=delivery_app_tree_identity(app)
        profile=_json_object(profile_receipt_path)
        component=verify_component_manifest(app,app.parent/MANIFEST_NAME)
        phase_digest,phase_files,phase_bytes=phase10_tree_digest(app)
        binding=profile.get("app_tree_binding",{})
        seal=profile.get("bundle_seal",{})
        if type(binding) is not dict or type(seal) is not dict:
            raise CandidateIdentityError("CANDIDATE_PROFILE_TREE_IDENTITY_MISSING")
        values=[first["sha256"],second["sha256"],profile.get("digests",{}).get("app_tree_sha256"),
            profile.get("app_tree",{}).get("sha256"),binding.get("first_recomputation"),
            binding.get("second_recomputation"),component["app_component_set_digest"],phase_digest]
        if (profile.get("ok") is not True or binding.get("stage")!="final_signed_v2"
            or seal.get("direct_codesign_returncode")!=0 or seal.get("strict_returncode")!=0
            or len(set(values))!=1 or first!=second or profile.get("app_tree")!=first
            or phase_files!=first["regular_file_count"] or phase_bytes!=first["regular_bytes"]):
            raise CandidateIdentityError("CANDIDATE_APP_TREE_CONSUMER_DRIFT")
        return {"schema_id":"lulu.generation9-candidate-identity-receipt/v2","ok":True,
            "identity":first,"first_recomputation":first,"second_recomputation":second,
            "component_verification":component,"consumer_bindings":values}
    first = canonical_app_tree_identity(app)
    second = canonical_app_tree_identity(app)
    if first != second:
        raise CandidateIdentityError("CANDIDATE_APP_TREE_RECOMPUTATION_DRIFT")

    profile = _json_object(profile_receipt_path)
    profile_digests = profile.get("digests")
    profile_tree = profile.get("app_tree")
    profile_binding = profile.get("app_tree_binding")
    if (
        type(profile_digests) is not dict
        or type(profile_tree) is not dict
        or type(profile_binding) is not dict
        or profile_binding.get("stage") != "post_materialization"
        or type(profile_binding.get("materialization_receipt_sha256")) is not str
        or len(profile_binding["materialization_receipt_sha256"]) != 64
    ):
        raise CandidateIdentityError("CANDIDATE_PROFILE_TREE_IDENTITY_MISSING")

    manifest_path = app / "Contents/Resources/agent_product_manifest.json"
    manifest = _json_object(manifest_path)
    component = verify_component_manifest(app, manifest_path)
    phase_digest, phase_files, phase_bytes = phase10_tree_digest(app)

    expected_sha = str(first["sha256"])
    bindings = {
        "canonical_first": expected_sha,
        "canonical_second": str(second.get("sha256")),
        "profile_digest": str(profile_digests.get("app_tree_sha256")),
        "profile_identity": str(profile_tree.get("sha256")),
        "profile_binding_first": str(profile_binding.get("first_recomputation")),
        "profile_binding_second": str(profile_binding.get("second_recomputation")),
        "component_manifest": str(manifest.get("app_component_set_digest")),
        "component_verifier": str(component.get("app_component_set_digest")),
        "phase10_finalizer": phase_digest,
    }
    if profile_tree != first or set(bindings.values()) != {expected_sha}:
        raise CandidateIdentityError("CANDIDATE_APP_TREE_CONSUMER_DRIFT")
    if (
        phase_files != first["regular_file_count"]
        or phase_bytes != first["regular_bytes"]
    ):
        raise CandidateIdentityError("CANDIDATE_APP_TREE_COUNT_DRIFT")

    return {
        "schema_id": SCHEMA_ID,
        "ok": True,
        "identity": first,
        "first_recomputation": first,
        "second_recomputation": second,
        "consumer_bindings": bindings,
        "phase10_counts": {
            "regular_file_count": phase_files,
            "regular_bytes": phase_bytes,
        },
        "component_verification": {
            "state": component.get("state"),
            "effect_count": component.get("effect_count"),
            "component_manifest_sha256": component.get("component_manifest_sha256"),
        },
    }


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        encoded = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--profile-receipt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    app = Path(args.app).resolve(strict=True)
    output = Path(args.output).resolve()
    if output == app or app in output.parents:
        raise CandidateIdentityError("CANDIDATE_IDENTITY_RECEIPT_INSIDE_APP")
    receipt = verify_candidate_identity(
        app=app,
        profile_receipt_path=Path(args.profile_receipt).resolve(strict=True),
    )
    write_receipt(output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
