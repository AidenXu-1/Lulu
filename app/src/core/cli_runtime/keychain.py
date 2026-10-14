"""Injectable Keychain adapters for CLI installation and ledger integrity."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from typing import Callable, Protocol
from uuid import uuid4

from .approval import (
    _commit_authoritative_installation_approval,
    _local_ticket_approval_metadata,
    _release_authoritative_installation_approval,
    _reserve_authoritative_installation_approval,
)
from .contracts import CliRuntimeTrustError, CodeSignatureEvidenceV1
from .installation_ticket import (
    ValidatedAuthority,
    _inventory_classification,
    _new_ownership_intents,
    _new_ownership_inventory_intents,
    _owned_item_claims,
    canonical_json_bytes,
    validate_authority,
)
from .installation_recovery import begin_recovery_attempt, append_recovery_event


INSTALLATION_TICKET_KEY = "lulu_agent_cli_installation_ticket_v1"
JOB_HMAC_KEY_PREFIX = "lulu_agent_cli_job_hmac_key_v1:"
JOB_ANCHOR_KEY_PREFIX = "lulu_agent_cli_job_anchor_v1:"
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EPOCH = re.compile(r"^[0-9a-f]{32}$")


class KeychainBoundary(Protocol):
    def get_text(self, key: str) -> str | None: ...

    def set_text(self, key: str, value: str) -> None: ...


class VersionedKeychainBoundary(Protocol):
    def get_text(self, key: str) -> str | None: ...

    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object: ...

    def create_owned_item(
        self,
        final_acl_intent_receipt: object,
        intent_receipt: object,
        secret_value: str,
    ) -> object: ...

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object: ...

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None: ...


class VersionedCredentialError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _secure_read(keychain: KeychainBoundary, key: str) -> str | None:
    try:
        return keychain.get_text(key)
    except Exception as exc:  # noqa: BLE001 - injected system boundary
        raise CliRuntimeTrustError(
            "SECURE_STORE_UNAVAILABLE", "CLI_KEYCHAIN_READ_FAILED"
        ) from exc


def _secure_write(keychain: KeychainBoundary, key: str, value: str) -> None:
    try:
        keychain.set_text(key, value)
    except Exception as exc:  # noqa: BLE001 - injected system boundary
        raise CliRuntimeTrustError(
            "SECURE_STORE_UNAVAILABLE", "CLI_KEYCHAIN_WRITE_FAILED"
        ) from exc


def load_installation_ticket(keychain: KeychainBoundary) -> dict[str, object]:
    raw = _secure_read(keychain, INSTALLATION_TICKET_KEY)
    if not raw:
        raise CliRuntimeTrustError(
            "TRUST_ANCHOR_INVALID", "CLI_INSTALLATION_TICKET_MISSING"
        )
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise CliRuntimeTrustError(
            "TRUST_ANCHOR_INVALID", "CLI_INSTALLATION_TICKET_INVALID"
        ) from exc
    if type(payload) is not dict:
        raise CliRuntimeTrustError(
            "TRUST_ANCHOR_INVALID", "CLI_INSTALLATION_TICKET_INVALID"
        )
    return payload


def provision_local_trusted_test_credentials(
    *,
    keychain: KeychainBoundary,
    bundle_id: str,
    source_commit: str,
    source_tree: str,
    canonical_cli_sha256: str,
    signature: CodeSignatureEvidenceV1,
    approval_receipt: object,
    hmac_key_factory: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    epoch_factory: Callable[[], str] = lambda: uuid4().hex,
) -> str:
    """Consume one Core-issued approval; commit the enabling ticket last."""

    if (
        type(bundle_id) is not str
        or bundle_id != "com.lulu.transcribe.trusted-test"
        or not _SOURCE_ID.fullmatch(source_commit)
        or not _SOURCE_ID.fullmatch(source_tree)
        or not _SHA256.fullmatch(canonical_cli_sha256)
        or not isinstance(signature, CodeSignatureEvidenceV1)
        or signature.signature_kind != "ad_hoc"
        or bool(signature.team_id)
    ):
        raise CliRuntimeTrustError(
            "TRUST_ANCHOR_INVALID", "LOCAL_TRUSTED_TEST_BINDING_INVALID"
        )
    reservation = _reserve_authoritative_installation_approval(
        approval_receipt,
        bundle_id=bundle_id,
        source_commit=source_commit,
        source_tree=source_tree,
        canonical_cli_sha256=canonical_cli_sha256,
        signature=signature,
        now=datetime.now(timezone.utc),
    )
    try:
        installation_id = reservation.claims.installation_id
        key = hmac_key_factory()
        epoch = epoch_factory()
        if (
            type(key) is not bytes
            or len(key) < 32
            or type(epoch) is not str
            or not _EPOCH.fullmatch(epoch)
        ):
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "INSTALLATION_CREDENTIAL_FACTORY_INVALID"
            )
        hmac_key_name = f"{JOB_HMAC_KEY_PREFIX}{installation_id}"
        anchor_key_name = f"{JOB_ANCHOR_KEY_PREFIX}{installation_id}"
        if any(
            _secure_read(keychain, item) is not None
            for item in (INSTALLATION_TICKET_KEY, hmac_key_name, anchor_key_name)
        ):
            raise CliRuntimeTrustError(
                "CONFLICT", "CLI_INSTALLATION_CREDENTIAL_ALREADY_EXISTS"
            )
        ticket = {
            "schema_id": "lulu.agent-cli-installation-ticket/v2",
            "schema_revision": 2,
            "trust_profile": "local_trusted_test",
            "bundle_id": bundle_id,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "canonical_cli_sha256": canonical_cli_sha256,
            "installation_id": installation_id,
            "signature_kind": signature.signature_kind,
            "certificate_sha256": signature.certificate_sha256,
            "cdhash": signature.cdhash,
            "designated_requirement": signature.designated_requirement,
            "team_id": signature.team_id,
            **_local_ticket_approval_metadata(reservation.claims),
        }
        _secure_write(
            keychain, hmac_key_name, base64.b64encode(key).decode("ascii")
        )
        _secure_write(
            keychain,
            anchor_key_name,
            json.dumps(
                {"epoch": epoch, "sequence": 0, "head_hmac": "0" * 64},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        _secure_write(
            keychain,
            INSTALLATION_TICKET_KEY,
            json.dumps(ticket, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        _release_authoritative_installation_approval(reservation)
        raise
    _commit_authoritative_installation_approval(reservation)
    return installation_id


def _versioned_read(keychain: VersionedKeychainBoundary, account: str) -> str | None:
    try:
        return keychain.get_text(account)
    except Exception as exc:  # noqa: BLE001 - injected Keychain boundary
        raise VersionedCredentialError("SECURE_STORE_UNAVAILABLE") from exc


def _create_owned_item(
    keychain: VersionedKeychainBoundary,
    final_acl_intent_receipt: object,
    intent_receipt: object,
    value: str,
) -> object:
    try:
        return keychain.create_owned_item(
            final_acl_intent_receipt,
            intent_receipt,
            value,
        )
    except Exception as exc:  # noqa: BLE001 - injected Keychain boundary
        reason = getattr(exc, "reason_code", None) or str(exc)
        if reason == "VERSIONED_ACCOUNT_CONFLICT":
            raise VersionedCredentialError("VERSIONED_ACCOUNT_CONFLICT") from exc
        raise VersionedCredentialError("INSTALLATION_RECOVERY_REQUIRED") from exc


def _versioned_ticket_payload(authority: ValidatedAuthority) -> dict[str, object]:
    candidate = authority.candidate
    return {
        "accounts": list(authority.accounts),
        "app_content_tree_sha256": candidate["app_content_tree_sha256"],
        "app_profile_tree_sha256": candidate["app_profile_tree_sha256"],
        "authority_artifact_sha256": authority.authority_artifact_sha256,
        "authority_identity": dict(authority.authority_identity),
        "build": candidate["build"],
        "bundle_id": candidate["bundle_id"],
        "channel": candidate["channel"],
        "carrier_sha256": authority.carrier_sha256,
        "canonical_cli_sha256": candidate["canonical_cli_sha256"],
        "installation_id": authority.installation_id,
        "installation_target": authority.installation_target,
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "product_version": candidate["product_version"],
        "product_name": candidate["product_name"],
        "profile": candidate["profile"],
        "schema_id": "lulu.agent-cli-installation-ticket/v3",
        "schema_revision": 3,
        "review_receipts": dict(authority.review_receipts),
        "signature": dict(candidate["signature"]),
        "source_identity": dict(authority.source_identity),
        "target_binding_sha256": authority.target_binding_sha256,
        "target_role": authority.target_role,
    }


def provision_versioned_installation_credentials(
    *,
    post_build_receipt: object,
    locator_receipt: object,
    plan_receipt: object,
    keychain: VersionedKeychainBoundary,
    hmac_key_factory: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    epoch_factory: Callable[[], str] = lambda: uuid4().hex,
    after_write: Callable[[int], None] = lambda _count: None,
    bind_created_item: Callable[[object], None] = lambda _receipt: None,
) -> dict[str, object]:
    """Create an authority-owned namespace in fixed HMAC/anchor/ticket order."""

    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
        post_build_receipt=post_build_receipt,
    )
    try:
        final_acl_intent = keychain.prepare_final_acl_intent(
            locator_receipt,
            plan_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - Core-private constructor boundary
        reason = getattr(exc, "reason_code", None) or str(exc)
        if reason not in {
            "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE",
            "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR",
        }:
            reason = "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE"
        raise VersionedCredentialError(reason) from exc
    inventory_intents = _new_ownership_inventory_intents(
        authority,
        final_acl_intent,
    )
    try:
        preflight = tuple(
            keychain.inspect_exact_item(final_acl_intent, intent)
            for intent in inventory_intents
        )
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason_code", None) or str(exc)
        if reason not in {
            "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR",
            "KEYCHAIN_OWNERSHIP_PROOF_REQUIRED",
        }:
            reason = "KEYCHAIN_OWNERSHIP_PROOF_REQUIRED"
        raise VersionedCredentialError(reason) from exc
    if any(
        _inventory_classification(
            item, authority=authority, account_index=index
        )
        != "absent"
        for index, item in enumerate(preflight)
    ):
        raise VersionedCredentialError("VERSIONED_ACCOUNT_CONFLICT")
    hmac_key = hmac_key_factory()
    epoch = epoch_factory()
    if (
        type(hmac_key) is not bytes
        or len(hmac_key) < 32
        or type(epoch) is not str
        or not _EPOCH.fullmatch(epoch)
    ):
        raise VersionedCredentialError("CANDIDATE_IDENTITY_MISMATCH")
    intents = _new_ownership_intents(authority, final_acl_intent)
    attempt_fingerprint = hashlib.sha256(
        _owned_attempt_id_from_intent(intents[0]).encode("utf-8")
    ).hexdigest()
    journal = begin_recovery_attempt(authority, attempt_fingerprint)
    ticket = canonical_json_bytes(_versioned_ticket_payload(authority)).decode("utf-8")
    values = (
        base64.b64encode(hmac_key).decode("ascii"),
        json.dumps(
            {"epoch": epoch, "sequence": 0, "head_hmac": "0" * 64},
            sort_keys=True,
            separators=(",", ":"),
        ),
        ticket,
    )
    created: list[object] = []
    for index, (account, value, intent) in enumerate(
        zip(authority.accounts, values, intents),
        start=1,
    ):
        try:
            receipt = _create_owned_item(
                keychain,
                final_acl_intent,
                intent,
                value,
            )
        except VersionedCredentialError as exc:
            if exc.reason_code == "VERSIONED_ACCOUNT_CONFLICT" and created:
                raise VersionedCredentialError(
                    "INSTALLATION_RECOVERY_REQUIRED"
                ) from exc
            raise
        created.append(receipt)
        append_recovery_event(
            journal=journal,
            authority=authority,
            attempt_fingerprint=attempt_fingerprint,
            stage=f"provision_write_{index}",
            account_index=index,
            account=account,
            outcome="succeeded",
        )
        try:
            after_write(index)
        except Exception as exc:  # noqa: BLE001 - crash/cancel seam
            raise VersionedCredentialError("INSTALLATION_RECOVERY_REQUIRED") from exc
    final_inventory = tuple(
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
        != "owned"
        for index, item in enumerate(final_inventory)
    ):
        raise VersionedCredentialError("INSTALLATION_RECOVERY_REQUIRED")
    attempts = {_owned_item_claims(item).provision_attempt_id for item in final_inventory}
    if attempts != {_owned_item_claims(created[0]).provision_attempt_id}:
        raise VersionedCredentialError("INSTALLATION_RECOVERY_REQUIRED")
    return {
        "schema": "lulu.agent-cli-versioned-provision-result/v1",
        "state": "provisioned_versioned",
        "accounts": list(authority.accounts),
        "installation_id": authority.installation_id,
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "target_binding_sha256": authority.target_binding_sha256,
        "target_role": authority.target_role,
        "installation_target": authority.installation_target,
        "ticket_written_last": True,
        "contains_secret_material": False,
    }


def _owned_attempt_id_from_intent(receipt: object) -> str:
    from .installation_ticket import _ownership_intent_claims

    value = _ownership_intent_claims(receipt).provision_attempt_id
    if type(value) is not str:
        raise VersionedCredentialError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
    return value


def load_versioned_installation_ticket(
    *,
    locator_receipt: object,
    plan_receipt: object,
    keychain: VersionedKeychainBoundary,
) -> dict[str, object]:
    """Read only the exact v3 account selected by authoritative receipts."""

    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    raw = _versioned_read(keychain, authority.accounts[2])
    if raw is None:
        raise VersionedCredentialError("VERSIONED_TICKET_REQUIRED")
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise VersionedCredentialError("VERSIONED_TICKET_REQUIRED") from exc
    expected = _versioned_ticket_payload(authority)
    if type(payload) is not dict or payload != expected or raw != canonical_json_bytes(expected).decode("utf-8"):
        raise VersionedCredentialError("CANDIDATE_IDENTITY_MISMATCH")
    return payload


class KeyringJobIntegrityAnchorStore:
    """JobLedgerStore anchor backed by the installation's Keychain records."""

    def __init__(self, keychain: KeychainBoundary, installation_id: str) -> None:
        if not installation_id:
            raise ValueError("installation_id is required")
        self._keychain = keychain
        self._hmac_key_name = f"{JOB_HMAC_KEY_PREFIX}{installation_id}"
        self._anchor_key_name = f"{JOB_ANCHOR_KEY_PREFIX}{installation_id}"

    def hmac_key(self) -> bytes:
        encoded = _secure_read(self._keychain, self._hmac_key_name)
        if not encoded:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "JOB_HMAC_KEY_MISSING"
            )
        try:
            key = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "JOB_HMAC_KEY_INVALID"
            ) from exc
        if len(key) < 32:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "JOB_HMAC_KEY_INVALID"
            )
        return key

    def load(self) -> dict[str, object] | None:
        raw = _secure_read(self._keychain, self._anchor_key_name)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "JOB_HIGH_WATER_INVALID"
            ) from exc
        if type(payload) is not dict:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "JOB_HIGH_WATER_INVALID"
            )
        return payload

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None:
        current = self.load()
        if current != expected:
            raise CliRuntimeTrustError(
                "INTEGRITY_ERROR", "JOB_HIGH_WATER_COMPARE_AND_SET_FAILED"
            )
        _secure_write(
            self._keychain,
            self._anchor_key_name,
            json.dumps(replacement, sort_keys=True, separators=(",", ":")),
        )
