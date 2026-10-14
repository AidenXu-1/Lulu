"""Idempotent recovery for partially written versioned installation credentials."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Protocol

from .installation_ticket import (
    RECOVERY_JOURNAL_FILE,
    ValidatedAuthority,
    _inventory_classification,
    _new_ownership_inventory_intents,
    _owned_item_claims,
    _protected_formal_app_snapshot,
    validate_authority,
)


class RecoveryKeychainBoundary(Protocol):
    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object: ...

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object: ...

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None: ...


class InstallationRecoveryError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _validate_journal(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED")


def _ensure_journal(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_journal(path)
        return
    except OSError as exc:
        raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED") from exc
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    _validate_journal(path)


def _append_event(path: Path, event: dict[str, object]) -> None:
    raw = (
        json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED") from exc


def _event(
    *,
    authority: ValidatedAuthority,
    attempt_fingerprint: str,
    stage: str,
    account_index: int | None,
    account_fingerprint: str,
    outcome: str,
    reason_code: str,
) -> dict[str, object]:
    return {
        "schema_id": "lulu.agent-cli-installation-ticket-recovery-event/v1",
        "attempt_fingerprint": attempt_fingerprint,
        "stage": stage,
        "account_index": account_index,
        "account_fingerprint": account_fingerprint,
        "outcome": outcome,
        "reason_code": reason_code,
        "plan_sha256": authority.plan_sha256,
    }


def begin_recovery_attempt(
    authority: ValidatedAuthority,
    attempt_fingerprint: str,
) -> Path:
    """Create/validate the secret-free journal before a credential write."""

    journal = Path(authority.plan_path).with_name(RECOVERY_JOURNAL_FILE)
    _ensure_journal(journal)
    _append_event(
        journal,
        _event(
            authority=authority,
            attempt_fingerprint=attempt_fingerprint,
            stage="planned",
            account_index=None,
            account_fingerprint="",
            outcome="ready",
            reason_code="",
        ),
    )
    _append_event(
        journal,
        _event(
            authority=authority,
            attempt_fingerprint=attempt_fingerprint,
            stage="attempt_started",
            account_index=None,
            account_fingerprint="",
            outcome="ready",
            reason_code="",
        ),
    )
    return journal


def append_recovery_event(
    *,
    journal: Path,
    authority: ValidatedAuthority,
    attempt_fingerprint: str,
    stage: str,
    account_index: int | None,
    account: str | None,
    outcome: str,
    reason_code: str = "",
) -> None:
    allowed = {
        "planned",
        "attempt_started",
        "provision_write_1",
        "provision_write_2",
        "provision_write_3",
        "ownership_inventory",
        "owned_item_deleted",
        "recovery_absence_verified",
        "credentials_recovered",
    }
    if stage not in allowed:
        raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED")
    fingerprint = (
        "" if account is None else hashlib.sha256(account.encode("utf-8")).hexdigest()
    )
    _append_event(
        journal,
        _event(
            authority=authority,
            attempt_fingerprint=attempt_fingerprint,
            stage=stage,
            account_index=account_index,
            account_fingerprint=fingerprint,
            outcome=outcome,
            reason_code=reason_code,
        ),
    )


def recover_versioned_installation_credentials(
    *,
    locator_receipt: object,
    plan_receipt: object,
    keychain: RecoveryKeychainBoundary,
) -> dict[str, object]:
    """Delete-or-prove-absent for all three accounts on every invocation."""

    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    if (
        authority.target_role != "trusted_test"
        or authority.candidate.get("target_role") != authority.target_role
        or authority.candidate.get("installation_target")
        != authority.installation_target
    ):
        raise InstallationRecoveryError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    try:
        formal_snapshot = _protected_formal_app_snapshot(
            Path(authority.installation_target).parent / "Lulu.app"
        )
    except Exception as exc:  # noqa: BLE001 - read-only protection boundary
        raise InstallationRecoveryError("FORMAL_APP_PROTECTION_DRIFT") from exc
    if formal_snapshot != authority.formal_app_snapshot:
        raise InstallationRecoveryError("FORMAL_APP_PROTECTION_DRIFT")
    try:
        final_acl_intent = keychain.prepare_final_acl_intent(
            locator_receipt,
            plan_receipt,
        )
        intents = _new_ownership_inventory_intents(authority, final_acl_intent)
        inventory = tuple(
            keychain.inspect_exact_item(final_acl_intent, intent)
            for intent in intents
        )
        classifications = tuple(
            _inventory_classification(
                item, authority=authority, account_index=index
            )
            for index, item in enumerate(inventory)
        )
        if "foreign_or_unproven" in classifications:
            raise InstallationRecoveryError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
        owned_indexes = tuple(
            index for index, state in enumerate(classifications) if state == "owned"
        )
        if owned_indexes not in ((), (0,), (0, 1), (0, 1, 2)):
            raise InstallationRecoveryError(
                "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR"
            )
        attempts = {
            _owned_item_claims(inventory[index]).provision_attempt_id
            for index in owned_indexes
        }
        if len(attempts) > 1:
            raise InstallationRecoveryError(
                "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR"
            )
        attempt_fingerprint = (
            hashlib.sha256(next(iter(attempts)).encode("utf-8")).hexdigest()
            if attempts
            else hashlib.sha256(
                (authority.plan_sha256 + ":absent").encode("utf-8")
            ).hexdigest()
        )
        journal = begin_recovery_attempt(authority, attempt_fingerprint)
        for index, account in enumerate(authority.accounts, start=1):
            append_recovery_event(
                journal=journal,
                authority=authority,
                attempt_fingerprint=attempt_fingerprint,
                stage="ownership_inventory",
                account_index=index,
                account=account,
                outcome=classifications[index - 1],
            )
        for zero_index in reversed(owned_indexes):
            keychain.delete_verified_item(final_acl_intent, inventory[zero_index])
            append_recovery_event(
                journal=journal,
                authority=authority,
                attempt_fingerprint=attempt_fingerprint,
                stage="owned_item_deleted",
                account_index=zero_index + 1,
                account=authority.accounts[zero_index],
                outcome="succeeded",
            )
        final = tuple(
            keychain.inspect_exact_item(final_acl_intent, intent)
            for intent in _new_ownership_inventory_intents(
                authority,
                final_acl_intent,
            )
        )
        if any(
            _inventory_classification(
                item, authority=authority, account_index=index
            )
            != "absent"
            for index, item in enumerate(final)
        ):
            raise InstallationRecoveryError("INSTALLATION_RECOVERY_REQUIRED")
        for index, account in enumerate(authority.accounts, start=1):
            append_recovery_event(
                journal=journal,
                authority=authority,
                attempt_fingerprint=attempt_fingerprint,
                stage="recovery_absence_verified",
                account_index=index,
                account=account,
                outcome="absent",
            )
        append_recovery_event(
            journal=journal,
            authority=authority,
            attempt_fingerprint=attempt_fingerprint,
            stage="credentials_recovered",
            account_index=None,
            account=None,
            outcome="complete",
        )
    except InstallationRecoveryError:
        raise
    except Exception as exc:  # noqa: BLE001 - injected Keychain boundary
        reason = getattr(exc, "reason_code", None) or str(exc)
        if reason not in {
            "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE",
            "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR",
        }:
            reason = "INSTALLATION_RECOVERY_REQUIRED"
        raise InstallationRecoveryError(reason) from exc
    return {
        "schema": "lulu.agent-cli-installation-recovery-result/v1",
        "state": "credentials_recovered",
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "journal_path": str(journal),
        "target_binding_sha256": authority.target_binding_sha256,
        "target_role": authority.target_role,
        "installation_target": authority.installation_target,
        "contains_secret_material": False,
    }
