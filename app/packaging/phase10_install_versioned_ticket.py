"""Phase 10 orchestration for one versioned Trusted Test installation ticket.

This module composes the frozen file transaction with the versioned credential
transaction.  It contains no direct Keychain, codesign, or installation calls.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import secrets
from typing import Callable

from src.core.cli_runtime.installation_recovery import (
    recover_versioned_installation_credentials,
)
from src.core.cli_runtime.installation_ticket import (
    _prepare_fixed_candidate_installation,
    validate_authority,
)
from src.core.cli_runtime.keychain import (
    VersionedKeychainBoundary,
    provision_versioned_installation_credentials,
)
from src.core.cli_runtime.macos_keychain_acl import (
    KeychainAclBoundary,
    bind_versioned_keychain_acl,
)
from src.core.cli_runtime.macos_signature import MacOSCodeSignatureVerifier
from packaging.phase10_post_build_authority import (
    _fixed_candidate_bundle_for_installer,
    load_fixed_post_build_authority,
)


_FROZEN_INSTALLER_SHA256 = (
    "ca0c4a8f190ec758866fc5900505d1ded237f8d1bc43e991c4bfdd3bd899082b"
)


class Phase10VersionedTicketError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _load_frozen_installer() -> dict[str, object]:
    path = Path(__file__).resolve().with_name("phase10_install_candidate.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != _FROZEN_INSTALLER_SHA256:
        raise Phase10VersionedTicketError("FROZEN_INSTALLER_IDENTITY_MISMATCH")
    namespace = runpy.run_path(str(path))
    if not callable(namespace.get("install_candidate")) or not callable(
        namespace.get("rollback_install")
    ):
        raise Phase10VersionedTicketError("FROZEN_INSTALLER_IDENTITY_MISMATCH")
    return namespace


def _install_versioned_ticket_transaction(
    *,
    keychain: VersionedKeychainBoundary,
    acl_boundary: KeychainAclBoundary,
    frozen_installer_loader: Callable[[], dict[str, object]] = _load_frozen_installer,
    hmac_key_factory: Callable[[], bytes],
    epoch_factory: Callable[[], str],
    after_write: Callable[[int], None] = lambda _count: None,
) -> dict[str, object]:
    """Private Fake-capable composition core for the fixed public entry."""

    try:
        post_build_receipt = load_fixed_post_build_authority()
        _fixed_candidate_bundle_for_installer(post_build_receipt)
        locator_receipt, plan_receipt = _prepare_fixed_candidate_installation(
            post_build_receipt=post_build_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - Core-owned authority boundary
        reason = getattr(exc, "reason_code", "INSTALLATION_RECOVERY_REQUIRED")
        raise Phase10VersionedTicketError(str(reason)) from exc
    installed = False
    try:
        validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
        namespace = frozen_installer_loader()
        install_candidate = namespace.get("install_candidate")
        rollback_install = namespace.get("rollback_install")
        if not callable(install_candidate) or not callable(rollback_install):
            raise Phase10VersionedTicketError("FROZEN_INSTALLER_IDENTITY_MISMATCH")
        install_result = install_candidate(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
        if type(install_result) is not dict or install_result.get("state") != "installed":
            raise Phase10VersionedTicketError("INSTALLATION_RECOVERY_REQUIRED")
        installed = True
        signature_verifier = MacOSCodeSignatureVerifier()

        def bind_created_item(item_receipt: object) -> None:
            bind_versioned_keychain_acl(
                locator_receipt=locator_receipt,
                plan_receipt=plan_receipt,
                signature_verifier=signature_verifier,
                acl_boundary=acl_boundary,
                item_receipt=item_receipt,
            )

        provision = provision_versioned_installation_credentials(
            post_build_receipt=post_build_receipt,
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
            keychain=keychain,
            hmac_key_factory=hmac_key_factory,
            epoch_factory=epoch_factory,
            after_write=after_write,
            bind_created_item=bind_created_item,
        )
        return {
            "schema": "lulu.phase10-versioned-ticket-install-result/v1",
            "state": "installed_versioned_ticket",
            "file_install_state": install_result["state"],
            "credential_state": provision["state"],
            "locator_sha256": provision["locator_sha256"],
            "plan_sha256": provision["plan_sha256"],
            "installation_target": provision["installation_target"],
            "target_role": provision["target_role"],
            "target_binding_sha256": provision["target_binding_sha256"],
            "ticket_written_last": True,
            "contains_secret_material": False,
        }
    except Exception as exc:  # noqa: BLE001 - composed transaction boundaries
        reason_code = getattr(exc, "reason_code", None)
        if reason_code != "VERSIONED_ACCOUNT_CONFLICT":
            try:
                recover_versioned_installation_credentials(
                    locator_receipt=locator_receipt,
                    plan_receipt=plan_receipt,
                    keychain=keychain,
                )
            except Exception as recovery_exc:  # noqa: BLE001
                raise Phase10VersionedTicketError(
                    getattr(
                        recovery_exc,
                        "reason_code",
                        "INSTALLATION_RECOVERY_REQUIRED",
                    )
                ) from recovery_exc
        if installed:
            try:
                rollback_install(
                    locator_receipt=locator_receipt,
                    plan_receipt=plan_receipt,
                )
            except Exception as rollback_exc:  # noqa: BLE001
                raise Phase10VersionedTicketError(
                    "INSTALLATION_RECOVERY_REQUIRED"
                ) from rollback_exc
        if isinstance(exc, Phase10VersionedTicketError):
            raise
        if reason_code in (
            "CANDIDATE_IDENTITY_MISMATCH",
            "VERSIONED_ACCOUNT_CONFLICT",
        ):
            raise Phase10VersionedTicketError(
                reason_code
            ) from exc
        raise Phase10VersionedTicketError("INSTALLATION_RECOVERY_REQUIRED") from exc


def install_versioned_ticket(
    *,
    keychain: VersionedKeychainBoundary,
    acl_boundary: KeychainAclBoundary,
) -> dict[str, object]:
    """Run the fixed Trusted Test install chain without caller-selected seams."""

    return _install_versioned_ticket_transaction(
        keychain=keychain,
        acl_boundary=acl_boundary,
        frozen_installer_loader=_load_frozen_installer,
        hmac_key_factory=lambda: secrets.token_bytes(32),
        epoch_factory=lambda: secrets.token_hex(16),
    )
