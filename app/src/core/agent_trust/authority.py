"""Host and platform authority verification seams with no production fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .canonical import domain_separated_sha256
from .contracts import AgentTrustError, capability_unavailable


HOST_INTENT_AUDIENCE = "com.lulu.transcribe.core/intent/v1"
PLATFORM_APPROVAL_AUDIENCE = "com.lulu.transcribe.core/approval/v1"
MAX_AUTHORITY_AGE_SECONDS = 120.0

_HOST_CLAIM_FIELDS = frozenset(
    {
        "issuer_id",
        "issuer_verification_key_id",
        "audience",
        "authenticated_principal_id",
        "authenticated_session_id",
        "actor_class",
        "input_source",
        "origin_event_id",
        "local_uid",
        "installation_id",
        "nonce",
        "issued_at",
        "expires_at",
        "monotonic_issued_at",
        "monotonic_expires_at",
        "intent_payload_digest",
        "replay_slot",
    }
)
_PLATFORM_CLAIM_FIELDS = frozenset(
    {
        "proof_version",
        "issuer_id",
        "issuer_verification_key_id",
        "audience",
        "local_uid",
        "installation_id",
        "nonce",
        "approval_id",
        "intent_payload_digest",
        "issued_at",
        "expires_at",
        "monotonic_issued_at",
        "monotonic_expires_at",
        "replay_slot",
    }
)


class AuthorityVerifier(Protocol):
    """External verifier seam; Phase 4 supplies no OS-backed implementation."""

    def verify(self, handle_ref: str) -> Mapping[str, object]: ...


class ReplayStore(Protocol):
    def consume(self, slot: str) -> None: ...


class UnavailableHostIntentVerifier:
    """The production default until a real host integration is separately authorized."""

    def verify(self, handle_ref: str) -> Mapping[str, object]:
        del handle_ref
        raise capability_unavailable("HOST_INTENT_VERIFIER_UNAVAILABLE")


class UnavailablePlatformHumanPresenceVerifier:
    """No real OS/platform proof implementation exists in Phase 4."""

    def verify(self, handle_ref: str) -> Mapping[str, object]:
        del handle_ref
        raise capability_unavailable("PLATFORM_PROOF_VERIFIER_UNAVAILABLE")


def _authority_integrity(reason_code: str) -> AgentTrustError:
    return AgentTrustError(
        "INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )


def _nonempty_string(value: object) -> bool:
    return type(value) is str and bool(value)


def _validate_time_window(
    claims: Mapping[str, object], *, now_wall: float, now_monotonic: float
) -> None:
    values = (
        claims.get("issued_at"),
        claims.get("expires_at"),
        claims.get("monotonic_issued_at"),
        claims.get("monotonic_expires_at"),
        now_wall,
        now_monotonic,
    )
    if any(type(value) not in {int, float} for value in values):
        raise _authority_integrity("AUTHORITY_TIME_INVALID")
    issued_at = float(claims["issued_at"])
    expires_at = float(claims["expires_at"])
    monotonic_issued_at = float(claims["monotonic_issued_at"])
    monotonic_expires_at = float(claims["monotonic_expires_at"])
    if not (
        0.0 <= expires_at - issued_at <= MAX_AUTHORITY_AGE_SECONDS
        and 0.0
        <= monotonic_expires_at - monotonic_issued_at
        <= MAX_AUTHORITY_AGE_SECONDS
        and issued_at <= now_wall <= expires_at
        and monotonic_issued_at <= now_monotonic <= monotonic_expires_at
        and now_wall - issued_at <= MAX_AUTHORITY_AGE_SECONDS
        and now_monotonic - monotonic_issued_at <= MAX_AUTHORITY_AGE_SECONDS
    ):
        raise _authority_integrity("AUTHORITY_FRESHNESS_INVALID")


def _validate_pinned_issuer(
    claims: Mapping[str, object], pinned_issuers: Mapping[str, frozenset[str]]
) -> None:
    issuer_id = claims.get("issuer_id")
    key_id = claims.get("issuer_verification_key_id")
    if not _nonempty_string(issuer_id) or not _nonempty_string(key_id):
        raise _authority_integrity("AUTHORITY_ISSUER_INVALID")
    pinned_keys = pinned_issuers.get(issuer_id)
    if not isinstance(pinned_keys, frozenset) or key_id not in pinned_keys:
        raise _authority_integrity("AUTHORITY_ISSUER_UNPINNED")


def verify_host_user_intent(
    *,
    handle_ref: str,
    verifier: AuthorityVerifier,
    replay_store: ReplayStore,
    pinned_issuers: Mapping[str, frozenset[str]],
    expected_intent_payload_digest: str,
    expected_local_uid: int,
    expected_installation_id: str,
    expected_nonce: str,
    now_wall: float,
    now_monotonic: float,
) -> dict[str, object]:
    """Verify a host handle; validation and replay consumption remain Core-owned."""
    if not _nonempty_string(handle_ref):
        raise _authority_integrity("HOST_HANDLE_INVALID")
    claims = verifier.verify(handle_ref)
    if not isinstance(claims, Mapping):
        raise _authority_integrity("HOST_INTENT_CLAIMS_INVALID")
    if frozenset(claims) != _HOST_CLAIM_FIELDS:
        raise _authority_integrity("HOST_INTENT_CLAIMS_INVALID")
    _validate_pinned_issuer(claims, pinned_issuers)
    if claims.get("audience") != HOST_INTENT_AUDIENCE:
        raise _authority_integrity("HOST_AUDIENCE_MISMATCH")
    if claims.get("actor_class") != "authenticated_human_user":
        raise _authority_integrity("HOST_ACTOR_CLASS_FORBIDDEN")
    if claims.get("input_source") != "host_authenticated_user_instruction":
        raise _authority_integrity("HOST_INPUT_SOURCE_FORBIDDEN")
    for field in (
        "authenticated_principal_id",
        "authenticated_session_id",
        "origin_event_id",
        "installation_id",
        "nonce",
        "intent_payload_digest",
        "replay_slot",
    ):
        if not _nonempty_string(claims.get(field)):
            raise _authority_integrity("HOST_PROVENANCE_INCOMPLETE")
    if claims.get("local_uid") != expected_local_uid:
        raise _authority_integrity("HOST_UID_MISMATCH")
    if claims.get("installation_id") != expected_installation_id:
        raise _authority_integrity("HOST_INSTALLATION_MISMATCH")
    if claims.get("nonce") != expected_nonce:
        raise _authority_integrity("HOST_NONCE_MISMATCH")
    if claims.get("intent_payload_digest") != expected_intent_payload_digest:
        raise _authority_integrity("HOST_INTENT_DIGEST_MISMATCH")
    _validate_time_window(claims, now_wall=now_wall, now_monotonic=now_monotonic)
    replay_store.consume(claims["replay_slot"])
    return {
        "issuer_type": "verified_host_user_intent",
        **dict(claims),
        "attestation_handle_ref": handle_ref,
        "attestation_handle_digest": domain_separated_sha256(
            "LULU_AUTHORITY_HANDLE_V1", {"handle_ref": handle_ref}
        ),
    }


def verify_platform_human_presence(
    *,
    handle_ref: str,
    verifier: AuthorityVerifier,
    pinned_issuers: Mapping[str, frozenset[str]],
    expected_approval_id: str,
    expected_intent_payload_digest: str,
    expected_local_uid: int,
    expected_installation_id: str,
    expected_nonce: str,
    now_wall: float,
    now_monotonic: float,
) -> dict[str, object]:
    """Verify proof claims without consuming them; ledger approval owns the burn."""
    if not _nonempty_string(handle_ref):
        raise _authority_integrity("PLATFORM_PROOF_HANDLE_INVALID")
    claims = verifier.verify(handle_ref)
    if not isinstance(claims, Mapping) or frozenset(claims) != _PLATFORM_CLAIM_FIELDS:
        raise _authority_integrity("PLATFORM_PROOF_CLAIMS_INVALID")
    _validate_pinned_issuer(claims, pinned_issuers)
    if claims.get("proof_version") != "HUMAN_PRESENCE_PROOF_V1":
        raise _authority_integrity("PLATFORM_PROOF_VERSION_MISMATCH")
    if claims.get("audience") != PLATFORM_APPROVAL_AUDIENCE:
        raise _authority_integrity("PLATFORM_PROOF_AUDIENCE_MISMATCH")
    for field in (
        "installation_id",
        "nonce",
        "approval_id",
        "intent_payload_digest",
        "replay_slot",
    ):
        if not _nonempty_string(claims.get(field)):
            raise _authority_integrity("PLATFORM_PROOF_CLAIMS_INVALID")
    if claims.get("local_uid") != expected_local_uid:
        raise _authority_integrity("PLATFORM_PROOF_UID_MISMATCH")
    if claims.get("installation_id") != expected_installation_id:
        raise _authority_integrity("PLATFORM_PROOF_INSTALLATION_MISMATCH")
    if claims.get("nonce") != expected_nonce:
        raise _authority_integrity("PLATFORM_PROOF_NONCE_MISMATCH")
    if claims.get("approval_id") != expected_approval_id:
        raise _authority_integrity("PLATFORM_PROOF_APPROVAL_MISMATCH")
    if claims.get("intent_payload_digest") != expected_intent_payload_digest:
        raise _authority_integrity("PLATFORM_PROOF_INTENT_DIGEST_MISMATCH")
    _validate_time_window(claims, now_wall=now_wall, now_monotonic=now_monotonic)
    return {
        "issuer_type": "platform_human_presence_fallback",
        **dict(claims),
        "proof_handle_ref": handle_ref,
        "proof_handle_digest": domain_separated_sha256(
            "LULU_AUTHORITY_HANDLE_V1", {"handle_ref": handle_ref}
        ),
    }
