"""Authoritative versioned installation-ticket and durable-plan contracts."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import stat
import sys
import tempfile
from threading import RLock


LEGACY_FIXED_TICKET = "lulu_agent_cli_installation_ticket_v1"
JOB_HMAC_KEY_PREFIX = "lulu_agent_cli_job_hmac_key_v1:"
JOB_ANCHOR_KEY_PREFIX = "lulu_agent_cli_job_anchor_v1:"
VERSIONED_TICKET_PREFIX = "lulu_agent_cli_installation_ticket_v3:"
PLAN_RELATIVE_ROOT = Path("Lulu/installation-trust/agent-cli")
PLAN_FILE = "ticket-plan.json"
RECOVERY_JOURNAL_FILE = "ticket-recovery.jsonl"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_CDHASH = re.compile(r"^[0-9a-f]{40,64}$")
_INSTALLATION_ID = re.compile(r"^lulu-tt-[0-9a-f]{32}$")
_POST_BUILD_SEAL = object()
_SECURITY_ARTIFACT_REVIEW_SEAL = object()
_TEST_ARTIFACT_REVIEW_SEAL = object()
_LOCATOR_SEAL = object()
_PLAN_SEAL = object()
_FINAL_ACL_INTENT_SEAL = object()
_OWNERSHIP_INTENT_SEAL = object()
_OWNERSHIP_ITEM_SEAL = object()
_LOCK = RLock()

_R16_IDENTITY = {
    "architecture": "arm64",
    "build": "20260823.1",
    "bundle_id": "com.lulu.transcribe.trusted-test",
    "channel": "trusted-test",
    "product_name": "Lulu Trusted Test",
    "profile": "trusted_test",
    "product_version": "0.3.2",
}
_TRUSTED_TEST_TARGET_IDENTITY = {
    "bundle_id": "com.lulu.transcribe.trusted-test",
    "channel": "trusted-test",
    "product_name": "Lulu Trusted Test",
    "profile": "trusted_test",
}
_TRUSTED_TEST_TARGET_ROLE = "trusted_test"
_TRUSTED_TEST_INSTALLATION_TARGET = "/Applications/Lulu Trusted Test.app"


class AuthorityError(RuntimeError):
    """A versioned installation authority failed closed."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _AuthoritativeLocatorReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _LOCATOR_SEAL:
            raise TypeError("authoritative locator receipt required")
        object.__setattr__(self, "_AuthoritativeLocatorReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("authoritative locator receipt is immutable")

    def __reduce__(self):
        raise TypeError("authoritative locator receipt is not serializable")


class _PostBuildAuthorityReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _POST_BUILD_SEAL:
            raise TypeError("post-build authority receipt required")
        object.__setattr__(self, "_PostBuildAuthorityReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("post-build authority receipt is immutable")

    def __reduce__(self):
        raise TypeError("post-build authority receipt is not serializable")


class _SecurityAuthorityArtifactReviewReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _SECURITY_ARTIFACT_REVIEW_SEAL:
            raise TypeError("security authority artifact review receipt required")
        object.__setattr__(
            self,
            "_SecurityAuthorityArtifactReviewReceipt__token",
            token,
        )

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("security authority artifact review receipt is immutable")

    def __reduce__(self):
        raise TypeError("security authority artifact review receipt is not serializable")


class _TestAuthorityArtifactReviewReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _TEST_ARTIFACT_REVIEW_SEAL:
            raise TypeError("test authority artifact review receipt required")
        object.__setattr__(
            self,
            "_TestAuthorityArtifactReviewReceipt__token",
            token,
        )

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("test authority artifact review receipt is immutable")

    def __reduce__(self):
        raise TypeError("test authority artifact review receipt is not serializable")


class _PersistedPlanReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _PLAN_SEAL:
            raise TypeError("persisted plan receipt required")
        object.__setattr__(self, "_PersistedPlanReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("persisted plan receipt is immutable")

    def __reduce__(self):
        raise TypeError("persisted plan receipt is not serializable")


class _OwnershipIntentReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _OWNERSHIP_INTENT_SEAL:
            raise TypeError("ownership intent receipt required")
        object.__setattr__(self, "_OwnershipIntentReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ownership intent receipt is immutable")

    def __reduce__(self):
        raise TypeError("ownership intent receipt is not serializable")


class _FinalKeychainACLIntentReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _FINAL_ACL_INTENT_SEAL:
            raise TypeError("final keychain ACL intent receipt required")
        object.__setattr__(self, "_FinalKeychainACLIntentReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("final keychain ACL intent receipt is immutable")

    def __reduce__(self):
        raise TypeError("final keychain ACL intent receipt is not serializable")


class _CreatedOwnedKeychainItemReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _OWNERSHIP_ITEM_SEAL:
            raise TypeError("created owned item receipt required")
        object.__setattr__(self, "_CreatedOwnedKeychainItemReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("created owned item receipt is immutable")

    def __reduce__(self):
        raise TypeError("created owned item receipt is not serializable")


class _VerifiedOwnedKeychainItemReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _OWNERSHIP_ITEM_SEAL:
            raise TypeError("verified owned item receipt required")
        object.__setattr__(self, "_VerifiedOwnedKeychainItemReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("verified owned item receipt is immutable")

    def __reduce__(self):
        raise TypeError("verified owned item receipt is not serializable")


class _ExactKeychainItemAbsentReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _OWNERSHIP_ITEM_SEAL:
            raise TypeError("exact absent item receipt required")
        object.__setattr__(self, "_ExactKeychainItemAbsentReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("exact absent item receipt is immutable")

    def __reduce__(self):
        raise TypeError("exact absent item receipt is not serializable")


class _ForeignOrUnprovenKeychainItem:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _OWNERSHIP_ITEM_SEAL:
            raise TypeError("foreign item classification required")
        object.__setattr__(self, "_ForeignOrUnprovenKeychainItem__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("foreign item classification is immutable")

    def __reduce__(self):
        raise TypeError("foreign item classification is not serializable")


@dataclass(frozen=True, slots=True)
class _LocatorClaims:
    candidate_bytes: bytes
    candidate_binding_sha256: str
    installation_id: str
    accounts: tuple[str, str, str]
    plan_path: str
    locator_sha256: str
    target_identity: tuple[tuple[str, str], ...]
    target_role: str
    installation_target: str
    target_binding_sha256: str
    formal_app_snapshot: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _PostBuildClaims:
    candidate_bytes: bytes
    carrier_path: str
    carrier_sha256: str
    authority_artifact_path: str
    authority_artifact_sha256: str
    review_receipts: tuple[tuple[str, str], ...]
    source_identity: tuple[tuple[str, str], ...]
    authority_identity: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _PlanClaims:
    post_build_token: str
    locator_token: str
    locator_sha256: str
    plan_path: str
    plan_sha256: str
    plan_size: int
    plan_bytes: bytes


@dataclass(frozen=True, slots=True)
class ValidatedAuthority:
    candidate_binding_sha256: str
    installation_id: str
    accounts: tuple[str, str, str]
    plan_path: str
    locator_sha256: str
    plan_sha256: str
    plan_size: int
    candidate: dict[str, object]
    carrier_sha256: str
    authority_artifact_sha256: str
    review_receipts: dict[str, str]
    source_identity: dict[str, str]
    authority_identity: dict[str, str]
    target_identity: dict[str, str]
    target_role: str
    installation_target: str
    target_binding_sha256: str
    formal_app_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class _OwnershipIntentClaims:
    authority: ValidatedAuthority
    account_index: int
    provision_attempt_id: str | None
    envelope: bytes | None
    expected_final_acl_sha256: str


@dataclass(frozen=True, slots=True)
class _FinalAclIntentClaims:
    authority: ValidatedAuthority
    canonical_bytes: bytes
    expected_final_acl_sha256: str
    access_ref: object | None
    platform: str
    architecture: str


@dataclass(frozen=True, slots=True)
class _OwnedItemClaims:
    authority: ValidatedAuthority
    account_index: int
    provision_attempt_id: str
    envelope: bytes
    persistent_ref: bytes
    attributes_sha256: str
    expected_final_acl_sha256: str


_CORE_POST_BUILD_REGISTRY: dict[str, _PostBuildClaims] = {}
_CORE_SECURITY_ARTIFACT_REVIEW_REGISTRY: dict[str, _PostBuildClaims] = {}
_CORE_TEST_ARTIFACT_REVIEW_REGISTRY: dict[str, _PostBuildClaims] = {}
_CORE_LOCATOR_REGISTRY: dict[str, _LocatorClaims] = {}
_CORE_PLAN_REGISTRY: dict[str, _PlanClaims] = {}
_CORE_FINAL_ACL_INTENT_REGISTRY: dict[str, _FinalAclIntentClaims] = {}
_CORE_OWNERSHIP_INTENT_REGISTRY: dict[str, _OwnershipIntentClaims] = {}
_CORE_OWNED_ITEM_REGISTRY: dict[str, _OwnedItemClaims] = {}
_CORE_ABSENT_ITEM_REGISTRY: dict[str, tuple[str, int]] = {}
_CORE_FOREIGN_ITEM_REGISTRY: dict[str, tuple[str, int]] = {}


def canonical_json_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _register_final_acl_intent(
    authority: ValidatedAuthority,
    *,
    canonical_bytes: bytes,
    access_ref: object | None,
    platform: str = "macOS",
    architecture: str = "arm64",
) -> object:
    if (
        type(authority) is not ValidatedAuthority
        or type(canonical_bytes) is not bytes
        or not canonical_bytes.endswith(b"\n")
        or platform != "macOS"
        or architecture != "arm64"
    ):
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
    try:
        payload = json.loads(canonical_bytes)
    except Exception as exc:
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR") from exc
    if (
        type(payload) is not dict
        or canonical_json_bytes(payload) != canonical_bytes
        or payload.get("schema_id") != "lulu.agent-cli-keychain-final-acl/v1"
        or payload.get("schema_revision") != 1
        or payload.get("platform") != platform
        or payload.get("architecture") != architecture
        or payload.get("entry_count") != 2
    ):
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
    try:
        entries = payload["entries"]
        owner = payload["owner"]
        if (
            type(entries) is not list
            or len(entries) != 2
            or [entry["entry_role"] for entry in entries]
            != ["partition_id", "trusted_application"]
            or owner
            != {
                "gid": os.getegid(),
                "owner_type": ["kSecUseOnlyUID"],
                "uid": os.geteuid(),
            }
        ):
            raise ValueError("final ACL shape")
        partition, trusted = entries
        if (
            partition["authorizations"]
            != ["kSecACLAuthorizationPartitionID"]
            or partition["description"]
            != "Lulu Agent CLI Trusted Test Partition"
            or partition["prompt_selector"] != 0
            or trusted["authorizations"] != ["kSecACLAuthorizationAny"]
            or trusted["description"] != "Lulu Agent CLI Trusted Test"
            or trusted["prompt_selector"] != 0
        ):
            raise ValueError("final ACL metadata")
        partition_apps = partition["trusted_applications"]
        trusted_apps = trusted["trusted_applications"]
        if (
            type(partition_apps) is not list
            or len(partition_apps) != 1
            or type(trusted_apps) is not list
            or len(trusted_apps) != 2
        ):
            raise ValueError("final ACL application count")
        expected_partition = (
            "cdhash:" + str(authority.candidate["signature"]["cdhash"])
        ).encode("utf-8")
        partition_app = partition_apps[0]
        if (
            partition_app["role"] != "cli_cdhash_partition"
            or partition_app["raw_data_utf8"].encode("utf-8")
            != expected_partition
            or partition_app["raw_data_length"] != len(expected_partition)
            or partition_app["raw_data_sha256"]
            != hashlib.sha256(expected_partition).hexdigest()
        ):
            raise ValueError("partition evidence")
        expected_paths = {
            "candidate_app": authority.installation_target,
            "installed_cli": str(
                Path(authority.installation_target)
                / "Contents/MacOS/lulu-agent-cli"
            ),
        }
        seen_roles: set[str] = set()
        seen_raw: set[bytes] = set()
        for application in trusted_apps:
            role = application["role"]
            raw = base64.b64decode(application["raw_data_b64"], validate=True)
            if (
                role not in expected_paths
                or role in seen_roles
                or raw in seen_raw
                or application["canonical_path"] != expected_paths[role]
                or application["raw_data_length"] != len(raw)
                or application["raw_data_sha256"]
                != hashlib.sha256(raw).hexdigest()
            ):
                raise ValueError("trusted application evidence")
            seen_roles.add(role)
            seen_raw.add(raw)
        if seen_roles != set(expected_paths):
            raise ValueError("trusted application roles")
    except Exception as exc:
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR") from exc
    token = secrets.token_urlsafe(32)
    claims = _FinalAclIntentClaims(
        authority=authority,
        canonical_bytes=canonical_bytes,
        expected_final_acl_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        access_ref=access_ref,
        platform=platform,
        architecture=architecture,
    )
    with _LOCK:
        _CORE_FINAL_ACL_INTENT_REGISTRY[token] = claims
    return _FinalKeychainACLIntentReceipt(_FINAL_ACL_INTENT_SEAL, token)


def _final_acl_intent_claims(
    receipt: object,
    *,
    authority: ValidatedAuthority | None = None,
) -> _FinalAclIntentClaims:
    if type(receipt) is not _FinalKeychainACLIntentReceipt:
        raise TypeError("final keychain ACL intent receipt required")
    token = object.__getattribute__(
        receipt, "_FinalKeychainACLIntentReceipt__token"
    )
    with _LOCK:
        claims = _CORE_FINAL_ACL_INTENT_REGISTRY.get(token)
    if claims is None:
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_REQUIRED")
    if authority is not None and claims.authority != authority:
        raise AuthorityError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
    return claims


def _ownership_envelope(
    authority: ValidatedAuthority,
    *,
    final_acl_intent_receipt: object,
    account_index: int,
    provision_attempt_id: str,
) -> bytes:
    final_acl = _final_acl_intent_claims(
        final_acl_intent_receipt,
        authority=authority,
    )
    account = authority.accounts[account_index]
    return canonical_json_bytes(
        {
            "account": account,
            "account_index": account_index,
            "authority_artifact_sha256": authority.authority_artifact_sha256,
            "candidate_binding_sha256": authority.candidate_binding_sha256,
            "carrier_sha256": authority.carrier_sha256,
            "contains_secret_material": False,
            "expected_final_acl_sha256": final_acl.expected_final_acl_sha256,
            "final_acl_architecture": final_acl.architecture,
            "final_acl_platform": final_acl.platform,
            "final_acl_schema_id": "lulu.agent-cli-keychain-final-acl/v1",
            "final_acl_schema_revision": 1,
            "installation_id": authority.installation_id,
            "installation_target": authority.installation_target,
            "locator_sha256": authority.locator_sha256,
            "plan_sha256": authority.plan_sha256,
            "provision_attempt_id": provision_attempt_id,
            "schema_id": "lulu.agent-cli-keychain-item-ownership/v1",
            "schema_revision": 1,
            "service": "Lulu",
            "target_binding_sha256": authority.target_binding_sha256,
            "target_role": authority.target_role,
            "ticket_written_last": True,
            "trusted_cli_cdhash": authority.candidate["signature"]["cdhash"],
            "trusted_cli_sha256": authority.candidate["canonical_cli_sha256"],
        }
    )


def _new_ownership_intents(
    authority: ValidatedAuthority,
    final_acl_intent_receipt: object,
) -> tuple[object, object, object]:
    """Mint one fresh attempt after the complete absent preflight."""

    final_acl = _final_acl_intent_claims(
        final_acl_intent_receipt,
        authority=authority,
    )
    attempt_id = secrets.token_hex(16)
    receipts: list[object] = []
    with _LOCK:
        for index in range(3):
            token = secrets.token_urlsafe(32)
            _CORE_OWNERSHIP_INTENT_REGISTRY[token] = _OwnershipIntentClaims(
                authority=authority,
                account_index=index,
                provision_attempt_id=attempt_id,
                envelope=_ownership_envelope(
                    authority,
                    final_acl_intent_receipt=final_acl_intent_receipt,
                    account_index=index,
                    provision_attempt_id=attempt_id,
                ),
                expected_final_acl_sha256=final_acl.expected_final_acl_sha256,
            )
            receipts.append(_OwnershipIntentReceipt(_OWNERSHIP_INTENT_SEAL, token))
    return tuple(receipts)  # type: ignore[return-value]


def _new_ownership_inventory_intents(
    authority: ValidatedAuthority,
    final_acl_intent_receipt: object,
) -> tuple[object, object, object]:
    final_acl = _final_acl_intent_claims(
        final_acl_intent_receipt,
        authority=authority,
    )
    receipts: list[object] = []
    with _LOCK:
        for index in range(3):
            token = secrets.token_urlsafe(32)
            _CORE_OWNERSHIP_INTENT_REGISTRY[token] = _OwnershipIntentClaims(
                authority=authority,
                account_index=index,
                provision_attempt_id=None,
                envelope=None,
                expected_final_acl_sha256=final_acl.expected_final_acl_sha256,
            )
            receipts.append(_OwnershipIntentReceipt(_OWNERSHIP_INTENT_SEAL, token))
    return tuple(receipts)  # type: ignore[return-value]


def _ownership_intent_claims(receipt: object) -> _OwnershipIntentClaims:
    if type(receipt) is not _OwnershipIntentReceipt:
        raise TypeError("ownership intent receipt required")
    token = object.__getattribute__(receipt, "_OwnershipIntentReceipt__token")
    with _LOCK:
        claims = _CORE_OWNERSHIP_INTENT_REGISTRY.get(token)
    if claims is None:
        raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
    return claims


def _register_created_owned_item(
    intent_receipt: object,
    final_acl_intent_receipt: object,
    *,
    persistent_ref: bytes,
    envelope: bytes,
    attributes_sha256: str,
) -> object:
    intent = _ownership_intent_claims(intent_receipt)
    final_acl = _final_acl_intent_claims(
        final_acl_intent_receipt,
        authority=intent.authority,
    )
    if (
        intent.provision_attempt_id is None
        or intent.envelope is None
        or type(persistent_ref) is not bytes
        or not persistent_ref
        or envelope != intent.envelope
        or final_acl.expected_final_acl_sha256
        != intent.expected_final_acl_sha256
        or not _SHA256.fullmatch(attributes_sha256)
    ):
        raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
    token = secrets.token_urlsafe(32)
    claims = _OwnedItemClaims(
        authority=intent.authority,
        account_index=intent.account_index,
        provision_attempt_id=intent.provision_attempt_id,
        envelope=envelope,
        persistent_ref=persistent_ref,
        attributes_sha256=attributes_sha256,
        expected_final_acl_sha256=final_acl.expected_final_acl_sha256,
    )
    with _LOCK:
        _CORE_OWNED_ITEM_REGISTRY[token] = claims
    return _CreatedOwnedKeychainItemReceipt(_OWNERSHIP_ITEM_SEAL, token)


def _register_inventory_result(
    intent_receipt: object,
    final_acl_intent_receipt: object,
    *,
    persistent_ref: bytes | None,
    envelope: bytes | None,
    attributes_sha256: str | None,
) -> object:
    intent = _ownership_intent_claims(intent_receipt)
    final_acl = _final_acl_intent_claims(
        final_acl_intent_receipt,
        authority=intent.authority,
    )
    if intent.provision_attempt_id is not None:
        raise TypeError("inventory intent receipt required")
    token = secrets.token_urlsafe(32)
    if persistent_ref is None and envelope is None and attributes_sha256 is None:
        with _LOCK:
            _CORE_ABSENT_ITEM_REGISTRY[token] = (
                intent.authority.plan_sha256,
                intent.account_index,
            )
        return _ExactKeychainItemAbsentReceipt(_OWNERSHIP_ITEM_SEAL, token)
    try:
        payload = json.loads(envelope) if type(envelope) is bytes else None
        attempt_id = payload.get("provision_attempt_id") if type(payload) is dict else None
        expected = (
            _ownership_envelope(
                intent.authority,
                final_acl_intent_receipt=final_acl_intent_receipt,
                account_index=intent.account_index,
                provision_attempt_id=attempt_id,
            )
            if type(attempt_id) is str and re.fullmatch(r"[0-9a-f]{32}", attempt_id)
            else None
        )
        valid = (
            type(persistent_ref) is bytes
            and bool(persistent_ref)
            and envelope == expected
            and final_acl.expected_final_acl_sha256
            == intent.expected_final_acl_sha256
            and type(attributes_sha256) is str
            and bool(_SHA256.fullmatch(attributes_sha256))
        )
    except Exception:
        valid = False
        attempt_id = None
    if not valid:
        with _LOCK:
            _CORE_FOREIGN_ITEM_REGISTRY[token] = (
                intent.authority.plan_sha256,
                intent.account_index,
            )
        return _ForeignOrUnprovenKeychainItem(_OWNERSHIP_ITEM_SEAL, token)
    claims = _OwnedItemClaims(
        authority=intent.authority,
        account_index=intent.account_index,
        provision_attempt_id=attempt_id,
        envelope=envelope,
        persistent_ref=persistent_ref,
        attributes_sha256=attributes_sha256,
        expected_final_acl_sha256=final_acl.expected_final_acl_sha256,
    )
    with _LOCK:
        _CORE_OWNED_ITEM_REGISTRY[token] = claims
    return _VerifiedOwnedKeychainItemReceipt(_OWNERSHIP_ITEM_SEAL, token)


def _owned_item_claims(receipt: object) -> _OwnedItemClaims:
    if type(receipt) not in (
        _CreatedOwnedKeychainItemReceipt,
        _VerifiedOwnedKeychainItemReceipt,
    ):
        raise TypeError("exact owned item receipt required")
    attribute = (
        "_CreatedOwnedKeychainItemReceipt__token"
        if type(receipt) is _CreatedOwnedKeychainItemReceipt
        else "_VerifiedOwnedKeychainItemReceipt__token"
    )
    token = object.__getattribute__(receipt, attribute)
    with _LOCK:
        claims = _CORE_OWNED_ITEM_REGISTRY.get(token)
    if claims is None:
        raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
    return claims


def _same_ownership_authority(
    left: ValidatedAuthority, right: ValidatedAuthority
) -> bool:
    return (
        left.plan_sha256 == right.plan_sha256
        and left.locator_sha256 == right.locator_sha256
        and left.authority_artifact_sha256 == right.authority_artifact_sha256
        and left.candidate_binding_sha256 == right.candidate_binding_sha256
        and left.installation_id == right.installation_id
        and left.target_binding_sha256 == right.target_binding_sha256
        and left.accounts == right.accounts
    )


def _inventory_classification(
    receipt: object,
    *,
    authority: ValidatedAuthority,
    account_index: int,
) -> str:
    if type(receipt) is _ExactKeychainItemAbsentReceipt:
        token = object.__getattribute__(
            receipt, "_ExactKeychainItemAbsentReceipt__token"
        )
        with _LOCK:
            binding = _CORE_ABSENT_ITEM_REGISTRY.get(token)
        if binding != (authority.plan_sha256, account_index):
            raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        return "absent"
    if type(receipt) is _VerifiedOwnedKeychainItemReceipt:
        claims = _owned_item_claims(receipt)
        if (
            claims.account_index != account_index
            or not _same_ownership_authority(claims.authority, authority)
        ):
            raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        return "owned"
    if type(receipt) is _ForeignOrUnprovenKeychainItem:
        token = object.__getattribute__(
            receipt, "_ForeignOrUnprovenKeychainItem__token"
        )
        with _LOCK:
            binding = _CORE_FOREIGN_ITEM_REGISTRY.get(token)
        if binding != (authority.plan_sha256, account_index):
            raise AuthorityError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        return "foreign_or_unproven"
    raise TypeError("exact inventory receipt required")


def _derive_installation_target(*, candidate: object) -> tuple[str, str]:
    """Resolve the one R20 physical target from verified candidate identity."""

    if type(candidate) is not dict:
        raise AuthorityError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    identity = {
        field: candidate.get(field) for field in _TRUSTED_TEST_TARGET_IDENTITY
    }
    if identity != _TRUSTED_TEST_TARGET_IDENTITY:
        raise AuthorityError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    return _TRUSTED_TEST_TARGET_ROLE, _TRUSTED_TEST_INSTALLATION_TARGET


def _validate_candidate(candidate: object) -> bytes:
    base_fields = {
        "architecture",
        "bundle_id",
        "product_version",
        "build",
        "channel",
        "product_name",
        "profile",
        "app_content_tree_sha256",
        "app_profile_tree_sha256",
        "canonical_cli_sha256",
        "carrier_sha256",
        "authority_artifact_sha256",
        "review_receipts",
        "source_identity",
        "authority_identity",
        "signature",
    }
    target_fields = {"target_role", "installation_target"}
    signature_fields = {
        "signature_kind",
        "certificate_sha256",
        "cdhash",
        "designated_requirement",
        "team_id",
    }
    if type(candidate) is not dict or set(candidate) not in {
        frozenset(base_fields),
        frozenset(base_fields | target_fields),
    }:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    signature = candidate.get("signature")
    if type(signature) is not dict or set(signature) != signature_fields:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    reviews = candidate.get("review_receipts")
    source = candidate.get("source_identity")
    authority = candidate.get("authority_identity")
    target_role, installation_target = _derive_installation_target(
        candidate=candidate
    )
    if (
        any(candidate.get(key) != value for key, value in _R16_IDENTITY.items())
        or not _SHA256.fullmatch(str(candidate.get("app_content_tree_sha256", "")))
        or not _SHA256.fullmatch(str(candidate.get("app_profile_tree_sha256", "")))
        or not _SHA256.fullmatch(str(candidate.get("canonical_cli_sha256", "")))
        or not _SHA256.fullmatch(str(candidate.get("carrier_sha256", "")))
        or not _SHA256.fullmatch(str(candidate.get("authority_artifact_sha256", "")))
        or type(reviews) is not dict
        or set(reviews) != {
            "security_app_carrier_sha256",
            "security_authority_artifact_sha256",
            "test_app_carrier_sha256",
            "test_authority_artifact_sha256",
        }
        or any(not _SHA256.fullmatch(str(value)) for value in reviews.values())
        or type(source) is not dict
        or set(source) != {
            "actual_build_source_commit",
            "actual_build_source_tree",
            "product_source_sha256",
            "reviewed_base_commit",
            "reviewed_base_tree",
        }
        or not _SOURCE_ID.fullmatch(str(source.get("actual_build_source_commit", "")))
        or not _SOURCE_ID.fullmatch(str(source.get("actual_build_source_tree", "")))
        or not _SHA256.fullmatch(str(source.get("product_source_sha256", "")))
        or not _SOURCE_ID.fullmatch(str(source.get("reviewed_base_commit", "")))
        or not _SOURCE_ID.fullmatch(str(source.get("reviewed_base_tree", "")))
        or type(authority) is not dict
        or set(authority) != {"artifact_sha256", "source_commit", "source_tree"}
        or authority.get("artifact_sha256") != candidate.get("authority_artifact_sha256")
        or not _SOURCE_ID.fullmatch(str(authority.get("source_commit", "")))
        or not _SOURCE_ID.fullmatch(str(authority.get("source_tree", "")))
        or not _CDHASH.fullmatch(str(signature.get("cdhash", "")))
        or signature.get("signature_kind") != "ad_hoc"
        or signature.get("certificate_sha256") != ""
        or signature.get("team_id") != ""
        or signature.get("designated_requirement")
        != f'cdhash H"{signature.get("cdhash")}"'
    ):
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    if target_fields.issubset(candidate) and (
        candidate.get("target_role") != target_role
        or candidate.get("installation_target") != installation_target
    ):
        raise AuthorityError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    return canonical_json_bytes(candidate)


def _bind_candidate_target(candidate_bytes: bytes) -> bytes:
    try:
        candidate = json.loads(candidate_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    if _validate_candidate(candidate) != candidate_bytes:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    target_role, installation_target = _derive_installation_target(
        candidate=candidate
    )
    bound = dict(candidate)
    bound["target_role"] = target_role
    bound["installation_target"] = installation_target
    return _validate_candidate(bound)


def _unbound_candidate_bytes(candidate: dict[str, object]) -> bytes:
    unbound = dict(candidate)
    unbound.pop("target_role", None)
    unbound.pop("installation_target", None)
    return _validate_candidate(unbound)


def _post_build_token(receipt: object) -> str:
    if type(receipt) is not _PostBuildAuthorityReceipt:
        raise TypeError("post-build authority receipt required")
    token = object.__getattribute__(
        receipt,
        "_PostBuildAuthorityReceipt__token",
    )
    if type(token) is not str:
        raise TypeError("post-build authority receipt required")
    return token


def _post_build_claims(receipt: object) -> tuple[str, _PostBuildClaims]:
    token = _post_build_token(receipt)
    with _LOCK:
        claims = _CORE_POST_BUILD_REGISTRY.get(token)
    if claims is None:
        raise AuthorityError("POST_BUILD_AUTHORITY_REQUIRED")
    _validate_post_build_claims(claims)
    return token, claims


def _validate_post_build_claims(claims: object) -> _PostBuildClaims:
    if type(claims) is not _PostBuildClaims:
        raise TypeError("post-build claims required")
    try:
        candidate = json.loads(claims.candidate_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AuthorityError("POST_BUILD_AUTHORITY_INTEGRITY_ERROR") from exc
    if (
        _validate_candidate(candidate) != claims.candidate_bytes
        or not _SHA256.fullmatch(claims.carrier_sha256)
        or not _SHA256.fullmatch(claims.authority_artifact_sha256)
        or dict(claims.authority_identity).get("artifact_sha256")
        != claims.authority_artifact_sha256
        or candidate.get("carrier_sha256") != claims.carrier_sha256
        or candidate.get("authority_artifact_sha256")
        != claims.authority_artifact_sha256
        or candidate.get("review_receipts") != dict(claims.review_receipts)
        or candidate.get("source_identity") != dict(claims.source_identity)
        or candidate.get("authority_identity") != dict(claims.authority_identity)
    ):
        raise AuthorityError("POST_BUILD_AUTHORITY_INTEGRITY_ERROR")
    return claims


def _issue_security_authority_artifact_review(claims: _PostBuildClaims) -> object:
    """Issue the Core-owned security review after independent byte review."""

    claims = _validate_post_build_claims(claims)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _CORE_SECURITY_ARTIFACT_REVIEW_REGISTRY[token] = claims
    return _SecurityAuthorityArtifactReviewReceipt(
        _SECURITY_ARTIFACT_REVIEW_SEAL,
        token,
    )


def _issue_test_authority_artifact_review(claims: _PostBuildClaims) -> object:
    """Issue the Core-owned test review after independent byte review."""

    claims = _validate_post_build_claims(claims)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _CORE_TEST_ARTIFACT_REVIEW_REGISTRY[token] = claims
    return _TestAuthorityArtifactReviewReceipt(_TEST_ARTIFACT_REVIEW_SEAL, token)


def _register_post_build_authority_reviews(
    security_review_receipt: object,
    test_review_receipt: object,
) -> object:
    """Register authority only from the two exact Core review receipt types."""

    if type(security_review_receipt) is not _SecurityAuthorityArtifactReviewReceipt:
        raise TypeError("security authority artifact review receipt required")
    if type(test_review_receipt) is not _TestAuthorityArtifactReviewReceipt:
        raise TypeError("test authority artifact review receipt required")
    security_token = object.__getattribute__(
        security_review_receipt,
        "_SecurityAuthorityArtifactReviewReceipt__token",
    )
    test_token = object.__getattribute__(
        test_review_receipt,
        "_TestAuthorityArtifactReviewReceipt__token",
    )
    with _LOCK:
        security_claims = _CORE_SECURITY_ARTIFACT_REVIEW_REGISTRY.get(
            security_token
        )
        test_claims = _CORE_TEST_ARTIFACT_REVIEW_REGISTRY.get(test_token)
    if security_claims is None or test_claims is None:
        raise AuthorityError("POST_BUILD_AUTHORITY_REQUIRED")
    if security_claims != test_claims:
        raise AuthorityError("POST_BUILD_AUTHORITY_INTEGRITY_ERROR")
    claims = _validate_post_build_claims(security_claims)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _CORE_POST_BUILD_REGISTRY[token] = claims
    return _PostBuildAuthorityReceipt(_POST_BUILD_SEAL, token)


def _accounts(installation_id: str) -> tuple[str, str, str]:
    if not _INSTALLATION_ID.fullmatch(installation_id):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    return (
        JOB_HMAC_KEY_PREFIX + installation_id,
        JOB_ANCHOR_KEY_PREFIX + installation_id,
        VERSIONED_TICKET_PREFIX + installation_id,
    )


def _locator_payload(
    *,
    candidate_binding_sha256: str,
    installation_id: str,
    accounts: tuple[str, str, str],
    plan_path: str,
    target_identity: tuple[tuple[str, str], ...],
    target_role: str,
    installation_target: str,
    target_binding_sha256: str,
    formal_app_snapshot: tuple[tuple[str, object], ...],
) -> dict[str, object]:
    return {
        "accounts": list(accounts),
        "candidate_binding_sha256": candidate_binding_sha256,
        "installation_id": installation_id,
        "plan_path": plan_path,
        "schema": "lulu.agent-cli-authoritative-locator-public/v1",
        "target_identity": dict(target_identity),
        "target_role": target_role,
        "installation_target": installation_target,
        "target_binding_sha256": target_binding_sha256,
        "formal_app_snapshot": dict(formal_app_snapshot),
    }


def _locator_sha256(**kwargs: object) -> str:
    return hashlib.sha256(canonical_json_bytes(_locator_payload(**kwargs))).hexdigest()


def _plan_payload(
    locator: _LocatorClaims,
    post_build: _PostBuildClaims,
) -> dict[str, object]:
    return {
        "accounts": list(locator.accounts),
        "authority_artifact_sha256": post_build.authority_artifact_sha256,
        "candidate": json.loads(locator.candidate_bytes.decode("utf-8")),
        "carrier_sha256": post_build.carrier_sha256,
        "contains_secret_material": False,
        "installation_id": locator.installation_id,
        "installation_target": locator.installation_target,
        "legacy_fixed_ticket_action": "preserve_presence_only",
        "locator_sha256": locator.locator_sha256,
        "review_receipts": dict(post_build.review_receipts),
        "schema": "lulu.r16-post-build-authority-ticket-plan/v1",
        "source_identity": dict(post_build.source_identity),
        "authority_identity": dict(post_build.authority_identity),
        "state": "planned_before_keychain_write",
        "target_binding_sha256": locator.target_binding_sha256,
        "target_identity": dict(locator.target_identity),
        "target_role": locator.target_role,
        "formal_app_snapshot": dict(locator.formal_app_snapshot),
        "ticket_written_last": True,
    }


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")


def _system_application_support_root() -> Path:
    """Resolve the one production support root from the real-uid account record."""

    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid)
        raw_home = account.pw_dir
        account_uid = account.pw_uid
    except (KeyError, OSError, AttributeError) as exc:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
    if (
        type(raw_home) is not str
        or not raw_home
        or type(account_uid) is not int
        or account_uid != uid
    ):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    home = Path(raw_home)
    if not home.is_absolute() or str(home) != os.path.normpath(raw_home):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    library = home / "Library"
    application_support = library / "Application Support"
    for path in (home, library, application_support):
        try:
            metadata = path.lstat()
        except (OSError, ValueError) as exc:
            raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != uid
        ):
            raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    _validate_directory(application_support)
    return application_support


def _application_support_root_from_authority(
    authority: ValidatedAuthority,
) -> Path:
    """Bind a runtime support root to both the account record and sealed plan."""

    application_support = _system_application_support_root()
    expected_plan = (
        application_support
        / PLAN_RELATIVE_ROOT
        / authority.candidate_binding_sha256
        / PLAN_FILE
    )
    if Path(authority.plan_path) != expected_plan:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    return application_support


def _managed_plan_directory(application_support_root: Path, binding: str) -> Path:
    root = Path(application_support_root)
    if not root.is_absolute():
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    _validate_directory(root)
    current = root
    for component in (*PLAN_RELATIVE_ROOT.parts, binding):
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
        _validate_directory(current)
    return current


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_plan_file(path: Path, expected: bytes) -> None:
    try:
        metadata = path.lstat()
        actual = path.read_bytes()
    except OSError as exc:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or actual != expected
    ):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")


def _new_locator_claims(
    *,
    candidate_bytes: bytes,
    application_support_root: Path,
    installation_id: str,
) -> _LocatorClaims:
    binding = hashlib.sha256(candidate_bytes).hexdigest()
    try:
        candidate = json.loads(candidate_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    if _validate_candidate(candidate) != candidate_bytes:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    target_role, installation_target = _derive_installation_target(
        candidate=candidate
    )
    if (
        candidate.get("target_role") != target_role
        or candidate.get("installation_target") != installation_target
    ):
        raise AuthorityError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    target_identity = tuple(sorted(_TRUSTED_TEST_TARGET_IDENTITY.items()))
    target_binding_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "candidate_binding_sha256": binding,
                "installation_id": installation_id,
                "installation_target": installation_target,
                "target_role": target_role,
            }
        )
    ).hexdigest()
    formal_app_snapshot = tuple(
        sorted(
            _protected_formal_app_snapshot(
                Path(installation_target).parent / "Lulu.app"
            ).items()
        )
    )
    plan_directory = _managed_plan_directory(application_support_root, binding)
    accounts = _accounts(installation_id)
    plan_path = str(plan_directory / PLAN_FILE)
    locator_sha = _locator_sha256(
        candidate_binding_sha256=binding,
        installation_id=installation_id,
        accounts=accounts,
        plan_path=plan_path,
        target_identity=target_identity,
        target_role=target_role,
        installation_target=installation_target,
        target_binding_sha256=target_binding_sha256,
        formal_app_snapshot=formal_app_snapshot,
    )
    return _LocatorClaims(
        candidate_bytes=candidate_bytes,
        candidate_binding_sha256=binding,
        installation_id=installation_id,
        accounts=accounts,
        plan_path=plan_path,
        locator_sha256=locator_sha,
        target_identity=target_identity,
        target_role=target_role,
        installation_target=installation_target,
        target_binding_sha256=target_binding_sha256,
        formal_app_snapshot=formal_app_snapshot,
    )


def _owned_bundle_entry(path: Path, *, directory: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not expected_kind(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    return metadata


def _measure_tree_bindings(bundle: Path) -> tuple[str, str]:
    content_records: list[bytes] = []
    profile_records: list[dict[str, object]] = []

    def visit(parent: Path) -> None:
        try:
            entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
        except OSError as exc:
            raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(bundle).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                content_records.append(f"D\0{relative}\0{mode:o}\n".encode("utf-8"))
                profile_records.append(
                    {"path": relative, "type": "dir", "mode": mode}
                )
                visit(path)
            elif stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(path)
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(bundle.resolve(strict=True))
                except (OSError, RuntimeError, ValueError) as exc:
                    raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
                if Path(target).is_absolute():
                    raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
                content_records.append(
                    f"L\0{relative}\0{mode:o}\0{target}\n".encode("utf-8")
                )
                profile_records.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "mode": mode,
                        "target": target,
                    }
                )
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
                digest = hashlib.sha256(raw).hexdigest()
                content_records.append(
                    f"F\0{relative}\0{mode:o}\0{len(raw)}\0{digest}\n".encode(
                        "utf-8"
                    )
                )
                profile_records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": mode,
                        "size": len(raw),
                        "sha256": digest,
                    }
                )
            else:
                raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")

    visit(bundle)
    content = hashlib.sha256(b"".join(sorted(content_records))).hexdigest()
    profile = hashlib.sha256(
        json.dumps(
            profile_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return content, profile


def _protected_formal_app_snapshot(path: Path) -> dict[str, object]:
    """Measure the formal App without ever treating it as an install target."""

    formal = Path(path)
    try:
        metadata = formal.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError as exc:
        raise AuthorityError("FORMAL_APP_PROTECTION_DRIFT") from exc
    if (
        formal.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise AuthorityError("FORMAL_APP_PROTECTION_DRIFT")
    try:
        content, profile = _measure_tree_bindings(formal)
        cli = formal / "Contents/MacOS/lulu-agent-cli"
        cli_sha256 = (
            hashlib.sha256(cli.read_bytes()).hexdigest()
            if cli.is_file() and not cli.is_symlink()
            else ""
        )
    except Exception as exc:  # noqa: BLE001 - protected read-only boundary
        if isinstance(exc, AuthorityError):
            raise
        raise AuthorityError("FORMAL_APP_PROTECTION_DRIFT") from exc
    return {
        "kind": "directory",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "content_tree_sha256": content,
        "profile_tree_sha256": profile,
        "canonical_cli_sha256": cli_sha256,
    }


def _read_owned_json(path: Path) -> dict[str, object]:
    _owned_bundle_entry(path, directory=False)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - fixed candidate bytes boundary
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    if type(value) is not dict:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")
    return value


def _validate_live_candidate(bundle: Path, candidate: dict[str, object]) -> None:
    """Remeasure the installed App without any embedded final artifact values."""

    _validate_candidate(candidate)
    target_role, installation_target = _derive_installation_target(
        candidate=candidate
    )
    if (
        target_role != _TRUSTED_TEST_TARGET_ROLE
        or candidate.get("target_role") != target_role
        or candidate.get("installation_target") != installation_target
        or bundle != Path(installation_target)
    ):
        raise AuthorityError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    _owned_bundle_entry(bundle, directory=True)
    contents = bundle / "Contents"
    cli = contents / "MacOS/lulu-agent-cli"
    info_path = contents / "Info.plist"
    resources = contents / "Resources"
    try:
        content_tree, profile_tree = _measure_tree_bindings(bundle)
        _owned_bundle_entry(cli, directory=False)
        cli_sha = hashlib.sha256(cli.read_bytes()).hexdigest()
        _owned_bundle_entry(info_path, directory=False)
        info = plistlib.loads(info_path.read_bytes())
        profile = _read_owned_json(resources / "build_profile.json")
        source = _read_owned_json(resources / "product_source.json")
        from .macos_signature import MacOSCodeSignatureVerifier

        evidence = MacOSCodeSignatureVerifier().verify(
            app_bundle=bundle,
            cli_path=cli,
        )
    except AuthorityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    source_identity = candidate["source_identity"]
    expected_signature = candidate["signature"]
    actual_signature = {
        "signature_kind": getattr(evidence, "signature_kind", None),
        "certificate_sha256": getattr(evidence, "certificate_sha256", None),
        "cdhash": getattr(evidence, "cdhash", None),
        "designated_requirement": getattr(evidence, "designated_requirement", None),
        "team_id": getattr(evidence, "team_id", None),
    }
    source_digest = hashlib.sha256(canonical_json_bytes(source).rstrip(b"\n")).hexdigest()
    if (
        content_tree != candidate["app_content_tree_sha256"]
        or profile_tree != candidate["app_profile_tree_sha256"]
        or cli_sha != candidate["canonical_cli_sha256"]
        or info.get("CFBundleIdentifier") != candidate["bundle_id"]
        or info.get("CFBundleShortVersionString") != candidate["product_version"]
        or info.get("CFBundleVersion") != candidate["build"]
        or profile.get("profile") != candidate["profile"]
        or profile.get("source_commit")
        != source_identity["actual_build_source_commit"]
        or profile.get("source_tree")
        != source_identity["actual_build_source_tree"]
        or source.get("source_commit")
        != source_identity["actual_build_source_commit"]
        or source.get("source_tree")
        != source_identity["actual_build_source_tree"]
        or source_digest != source_identity["product_source_sha256"]
        or actual_signature != expected_signature
    ):
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH")


def _prepare_fixed_candidate_installation(
    *,
    post_build_receipt: object,
) -> tuple[object, object]:
    """Persist a plan only from the exact bundle-external authority receipt."""

    post_build_token, post_build = _post_build_claims(post_build_receipt)
    nonce = secrets.token_urlsafe(24)
    candidate_bytes = _bind_candidate_target(post_build.candidate_bytes)
    installation_id = "lulu-tt-" + hashlib.sha256(
        candidate_bytes + nonce.encode("utf-8")
    ).hexdigest()[:32]
    locator = _new_locator_claims(
        candidate_bytes=candidate_bytes,
        application_support_root=_system_application_support_root(),
        installation_id=installation_id,
    )
    path = Path(locator.plan_path)
    raw = canonical_json_bytes(_plan_payload(locator, post_build))
    if path.exists() or path.is_symlink():
        raise AuthorityError("INSTALLATION_PLAN_CONFLICT")
    temporary: Path | None = None
    cleanup_error: OSError | None = None
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise AuthorityError("INSTALLATION_PLAN_CONFLICT") from exc
        except OSError as exc:
            raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
                _fsync_directory(path.parent)
            except OSError as exc:
                cleanup_error = exc
    if cleanup_error is not None:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from cleanup_error
    _validate_plan_file(path, raw)
    locator_token = secrets.token_urlsafe(32)
    plan_token = secrets.token_urlsafe(32)
    plan = _PlanClaims(
        post_build_token=post_build_token,
        locator_token=locator_token,
        locator_sha256=locator.locator_sha256,
        plan_path=locator.plan_path,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        plan_size=len(raw),
        plan_bytes=raw,
    )
    with _LOCK:
        _CORE_LOCATOR_REGISTRY[locator_token] = locator
        _CORE_PLAN_REGISTRY[plan_token] = plan
    return (
        _AuthoritativeLocatorReceipt(_LOCATOR_SEAL, locator_token),
        _PersistedPlanReceipt(_PLAN_SEAL, plan_token),
    )


def _reload_fixed_installed_authority() -> tuple[object, object]:
    """Rebuild runtime authority from canonical plan, v3 binding, and live App."""

    executable = Path(sys.executable).resolve(strict=True)
    try:
        bundle = executable.parents[2]
    except IndexError as exc:
        raise AuthorityError("CANDIDATE_IDENTITY_MISMATCH") from exc
    application_support = _system_application_support_root()
    plan_root = application_support / PLAN_RELATIVE_ROOT
    _validate_directory(plan_root)
    matches: list[tuple[Path, bytes, dict[str, object], bytes, _PostBuildClaims]] = []
    for directory in plan_root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
        _validate_directory(directory)
        path = directory / PLAN_FILE
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
            candidate = payload.get("candidate") if type(payload) is dict else None
            candidate_bytes = _validate_candidate(candidate)
            if directory.name != hashlib.sha256(candidate_bytes).hexdigest():
                continue
            _validate_live_candidate(bundle, candidate)
            post_build_candidate_bytes = _unbound_candidate_bytes(candidate)
            post_build = _PostBuildClaims(
                candidate_bytes=post_build_candidate_bytes,
                carrier_path="",
                carrier_sha256=str(candidate["carrier_sha256"]),
                authority_artifact_path="",
                authority_artifact_sha256=str(candidate["authority_artifact_sha256"]),
                review_receipts=tuple(sorted(candidate["review_receipts"].items())),
                source_identity=tuple(sorted(candidate["source_identity"].items())),
                authority_identity=tuple(sorted(candidate["authority_identity"].items())),
            )
            matches.append((path, raw, payload, candidate_bytes, post_build))
        except AuthorityError:
            continue
        except Exception:
            continue
    if len(matches) != 1:
        raise AuthorityError("VERSIONED_TICKET_REQUIRED")
    path, raw, payload, candidate_bytes, post_build = matches[0]
    _validate_plan_file(path, raw)
    installation_id = payload.get("installation_id")
    if type(installation_id) is not str:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    locator = _new_locator_claims(
        candidate_bytes=candidate_bytes,
        application_support_root=application_support,
        installation_id=installation_id,
    )
    expected = canonical_json_bytes(_plan_payload(locator, post_build))
    if raw != expected:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    post_build_token = secrets.token_urlsafe(32)
    locator_token = secrets.token_urlsafe(32)
    plan_token = secrets.token_urlsafe(32)
    plan = _PlanClaims(
        post_build_token=post_build_token,
        locator_token=locator_token,
        locator_sha256=locator.locator_sha256,
        plan_path=locator.plan_path,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        plan_size=len(raw),
        plan_bytes=raw,
    )
    with _LOCK:
        _CORE_POST_BUILD_REGISTRY[post_build_token] = post_build
        _CORE_LOCATOR_REGISTRY[locator_token] = locator
        _CORE_PLAN_REGISTRY[plan_token] = plan
    return (
        _AuthoritativeLocatorReceipt(_LOCATOR_SEAL, locator_token),
        _PersistedPlanReceipt(_PLAN_SEAL, plan_token),
    )


def _locator_token(receipt: object) -> str:
    if type(receipt) is not _AuthoritativeLocatorReceipt:
        raise TypeError("authoritative locator receipt required")
    token = object.__getattribute__(receipt, "_AuthoritativeLocatorReceipt__token")
    if type(token) is not str:
        raise TypeError("authoritative locator receipt required")
    return token


def _plan_token(receipt: object) -> str:
    if type(receipt) is not _PersistedPlanReceipt:
        raise TypeError("persisted plan receipt required")
    token = object.__getattribute__(receipt, "_PersistedPlanReceipt__token")
    if type(token) is not str:
        raise TypeError("persisted plan receipt required")
    return token


def _locator_claims(receipt: object) -> tuple[str, _LocatorClaims]:
    token = _locator_token(receipt)
    with _LOCK:
        claims = _CORE_LOCATOR_REGISTRY.get(token)
    if claims is None:
        raise AuthorityError("AUTHORITATIVE_PLAN_REQUIRED")
    return token, claims


def validate_authority(
    *,
    locator_receipt: object,
    plan_receipt: object,
    post_build_receipt: object | None = None,
) -> ValidatedAuthority:
    locator_token, locator = _locator_claims(locator_receipt)
    token = _plan_token(plan_receipt)
    with _LOCK:
        plan = _CORE_PLAN_REGISTRY.get(token)
    if (
        plan is None
        or plan.locator_token != locator_token
        or plan.locator_sha256 != locator.locator_sha256
        or plan.plan_path != locator.plan_path
    ):
        raise AuthorityError("AUTHORITATIVE_PLAN_REQUIRED")
    with _LOCK:
        post_build = _CORE_POST_BUILD_REGISTRY.get(plan.post_build_token)
    if post_build is None:
        raise AuthorityError("POST_BUILD_AUTHORITY_REQUIRED")
    if (
        post_build_receipt is not None
        and _post_build_token(post_build_receipt) != plan.post_build_token
    ):
        raise AuthorityError("POST_BUILD_AUTHORITY_REQUIRED")
    expected = canonical_json_bytes(_plan_payload(locator, post_build))
    _validate_plan_file(Path(plan.plan_path), expected)
    if (
        expected != plan.plan_bytes
        or len(expected) != plan.plan_size
        or hashlib.sha256(expected).hexdigest() != plan.plan_sha256
    ):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    return ValidatedAuthority(
        candidate_binding_sha256=locator.candidate_binding_sha256,
        installation_id=locator.installation_id,
        accounts=locator.accounts,
        plan_path=locator.plan_path,
        locator_sha256=locator.locator_sha256,
        plan_sha256=plan.plan_sha256,
        plan_size=plan.plan_size,
        candidate=json.loads(locator.candidate_bytes.decode("utf-8")),
        carrier_sha256=post_build.carrier_sha256,
        authority_artifact_sha256=post_build.authority_artifact_sha256,
        review_receipts=dict(post_build.review_receipts),
        source_identity=dict(post_build.source_identity),
        authority_identity=dict(post_build.authority_identity),
        target_identity=dict(locator.target_identity),
        target_role=locator.target_role,
        installation_target=locator.installation_target,
        target_binding_sha256=locator.target_binding_sha256,
        formal_app_snapshot=dict(locator.formal_app_snapshot),
    )


def locator_public_fingerprint(receipt: object) -> dict[str, object]:
    _token, locator = _locator_claims(receipt)
    return {
        "schema": "lulu.agent-cli-authoritative-locator-public/v1",
        "candidate_binding_sha256": locator.candidate_binding_sha256,
        "locator_sha256": locator.locator_sha256,
        "plan_path": locator.plan_path,
        "target_identity": dict(locator.target_identity),
        "target_role": locator.target_role,
        "installation_target": locator.installation_target,
        "target_binding_sha256": locator.target_binding_sha256,
        "formal_app_snapshot": dict(locator.formal_app_snapshot),
    }


def plan_public_fingerprint(
    *, locator_receipt: object, plan_receipt: object
) -> dict[str, object]:
    claims = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    return {
        "schema": "lulu.agent-cli-authoritative-plan-public/v1",
        "locator_sha256": claims.locator_sha256,
        "plan_path": claims.plan_path,
        "plan_sha256": claims.plan_sha256,
        "plan_size": claims.plan_size,
        "mode": "0600",
        "contains_secret_material": False,
        "target_role": claims.target_role,
        "installation_target": claims.installation_target,
        "target_binding_sha256": claims.target_binding_sha256,
    }
