"""Fail-closed Keychain ACL binding for the versioned Agent CLI namespace."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import platform
import stat
import sys
from typing import Protocol

from .contracts import CodeSignatureEvidenceV1
from .installation_ticket import (
    _validate_live_candidate,
    _owned_item_claims,
    _register_final_acl_intent,
    canonical_json_bytes,
    validate_authority,
)


def _canonical_test_final_acl(authority: object) -> bytes:
    """Deterministic Fake boundary; production uses native trusted-app bytes."""

    app_path = str(authority.installation_target)
    cli_path = str(Path(app_path) / "Contents/MacOS/lulu-agent-cli")
    cdhash = str(authority.candidate["signature"]["cdhash"])

    def path_entry(role: str, path: str) -> dict[str, object]:
        raw = path.encode("utf-8")
        return {
            "canonical_path": path,
            "raw_data_b64": base64.b64encode(raw).decode("ascii"),
            "raw_data_length": len(raw),
            "raw_data_sha256": hashlib.sha256(raw).hexdigest(),
            "role": role,
        }

    partition = ("cdhash:" + cdhash).encode("utf-8")
    payload = {
        "architecture": "arm64",
        "entries": [
            {
                "authorizations": ["kSecACLAuthorizationPartitionID"],
                "description": "Lulu Agent CLI Trusted Test Partition",
                "entry_role": "partition_id",
                "prompt_selector": 0,
                "trusted_applications": [
                    {
                        "raw_data_length": len(partition),
                        "raw_data_sha256": hashlib.sha256(partition).hexdigest(),
                        "raw_data_utf8": partition.decode("utf-8"),
                        "role": "cli_cdhash_partition",
                    }
                ],
            },
            {
                "authorizations": ["kSecACLAuthorizationAny"],
                "description": "Lulu Agent CLI Trusted Test",
                "entry_role": "trusted_application",
                "prompt_selector": 0,
                "trusted_applications": sorted(
                    (
                        path_entry("candidate_app", app_path),
                        path_entry("installed_cli", cli_path),
                    ),
                    key=lambda item: (str(item["role"]), str(item["raw_data_b64"])),
                ),
            },
        ],
        "entry_count": 2,
        "owner": {
            "gid": os.getegid(),
            "owner_type": ["kSecUseOnlyUID"],
            "uid": os.geteuid(),
        },
        "platform": "macOS",
        "schema_id": "lulu.agent-cli-keychain-final-acl/v1",
        "schema_revision": 1,
    }
    return canonical_json_bytes(payload)


def _issue_memory_final_acl_intent(
    *,
    locator_receipt: object,
    plan_receipt: object,
) -> object:
    """Issue the exact opaque receipt for the production Memory test double."""

    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    return _register_final_acl_intent(
        authority,
        canonical_bytes=_canonical_test_final_acl(authority),
        access_ref=None,
    )


def _issue_native_final_acl_intent(
    *,
    locator_receipt: object,
    plan_receipt: object,
    native_boundary: object,
) -> object:
    """Build the sole native final access before the first credential write."""

    if (
        sys.platform != "darwin"
        or platform.machine() != "arm64"
        or platform.mac_ver()[0].split(".", 1)[0] != "15"
    ):
        raise KeychainAclBindingError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    try:
        _validate_live_candidate(
            Path(authority.installation_target),
            authority.candidate,
        )
        canonical_bytes, access_ref = native_boundary.construct_final_acl(authority)
    except Exception as exc:  # noqa: BLE001 - isolated native boundary
        reason = getattr(exc, "reason_code", None) or str(exc)
        if reason != "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR":
            reason = "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE"
        raise KeychainAclBindingError(reason) from exc
    if access_ref is None:
        raise KeychainAclBindingError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
    return _register_final_acl_intent(
        authority,
        canonical_bytes=canonical_bytes,
        access_ref=access_ref,
    )


class KeychainAclBoundary(Protocol):
    def verify_owned_item(self, *, item_receipt: object, cdhash: str) -> None: ...


class SignatureVerifierBoundary(Protocol):
    def verify(
        self,
        *,
        app_bundle: Path,
        cli_path: Path,
    ) -> CodeSignatureEvidenceV1: ...


class KeychainAclBindingError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _validated_cli_path(app_bundle: Path, cli_path: Path) -> tuple[Path, Path]:
    app = Path(app_bundle)
    cli = Path(cli_path)
    if not app.is_absolute() or not cli.is_absolute():
        raise KeychainAclBindingError("CANDIDATE_IDENTITY_MISMATCH")
    try:
        app = app.resolve(strict=True)
        cli = cli.resolve(strict=True)
        metadata = cli.lstat()
    except OSError as exc:
        raise KeychainAclBindingError("CANDIDATE_IDENTITY_MISMATCH") from exc
    expected = app / "Contents/MacOS/lulu-agent-cli"
    if (
        cli != expected
        or cli.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise KeychainAclBindingError("CANDIDATE_IDENTITY_MISMATCH")
    return app, cli


def _signature_dict(evidence: CodeSignatureEvidenceV1) -> dict[str, str]:
    return {
        "signature_kind": evidence.signature_kind,
        "certificate_sha256": evidence.certificate_sha256,
        "cdhash": evidence.cdhash,
        "designated_requirement": evidence.designated_requirement,
        "team_id": evidence.team_id,
    }


def bind_versioned_keychain_acl(
    *,
    locator_receipt: object,
    plan_receipt: object,
    signature_verifier: SignatureVerifierBoundary,
    acl_boundary: KeychainAclBoundary,
    item_receipt: object,
) -> dict[str, object]:
    """Bind exactly one just-created/live-verified receipt after live checks."""

    try:
        authority = validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
        item_claims = _owned_item_claims(item_receipt)
        if (
            item_claims.authority.plan_sha256 != authority.plan_sha256
            or item_claims.authority.target_binding_sha256
            != authority.target_binding_sha256
        ):
            raise KeychainAclBindingError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        if (
            authority.target_role != "trusted_test"
            or authority.target_identity
            != {
                "bundle_id": "com.lulu.transcribe.trusted-test",
                "channel": "trusted-test",
                "product_name": "Lulu Trusted Test",
                "profile": "trusted_test",
            }
            or authority.candidate.get("installation_target")
            != authority.installation_target
            or authority.candidate.get("target_role") != authority.target_role
        ):
            raise KeychainAclBindingError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
        app = Path(authority.installation_target)
        cli = app / "Contents/MacOS/lulu-agent-cli"
        app, cli = _validated_cli_path(app, cli)
        cli_sha256 = hashlib.sha256(cli.read_bytes()).hexdigest()
        evidence = signature_verifier.verify(app_bundle=app, cli_path=cli)
        if (
            not isinstance(evidence, CodeSignatureEvidenceV1)
            or cli_sha256 != authority.candidate["canonical_cli_sha256"]
            or _signature_dict(evidence) != authority.candidate["signature"]
        ):
            raise KeychainAclBindingError("CANDIDATE_IDENTITY_MISMATCH")
    except KeychainAclBindingError:
        raise
    except Exception as exc:  # noqa: BLE001 - injected verification boundary
        raise KeychainAclBindingError("CANDIDATE_IDENTITY_MISMATCH") from exc

    try:
        acl_boundary.verify_owned_item(
            item_receipt=item_receipt,
            cdhash=evidence.cdhash,
        )
    except Exception as exc:  # noqa: BLE001 - injected ACL boundary
        raise KeychainAclBindingError("INSTALLATION_RECOVERY_REQUIRED") from exc

    return {
        "schema": "lulu.agent-cli-keychain-acl-bind-result/v1",
        "state": "bound",
        "account_fingerprint": hashlib.sha256(
            authority.accounts[item_claims.account_index].encode("utf-8")
        ).hexdigest(),
        "cdhash": evidence.cdhash,
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "target_binding_sha256": authority.target_binding_sha256,
        "target_role": authority.target_role,
        "installation_target": authority.installation_target,
        "contains_secret_material": False,
    }
