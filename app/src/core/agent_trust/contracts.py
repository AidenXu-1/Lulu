"""Closed error and result contracts for the Phase 4 trust boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntentProposalV1:
    proposal_version: str
    producer: str
    trust_level: str
    plan_id: str
    intent_payload: dict[str, object]
    current_cli_state: str
    approved: bool
    dispatchable: bool
    intent_payload_digest: str
    proposal_digest: str

    def as_dict(self) -> dict[str, object]:
        return deepcopy(asdict(self))


@dataclass(frozen=True)
class TrustedIntentEnvelopeV1:
    envelope_version: str
    constructor_mode: str
    issuer_type: str
    issuer_id: str
    issuer_verification_key_id: str | None
    audience: str
    authenticated_principal_id: str | None
    authenticated_session_id: str | None
    actor_class: str
    input_source: str
    local_uid: int
    installation_id: str
    origin_event_id: str
    nonce: str
    issued_at: object
    expires_at: object
    capability_id: str
    command_id: str
    normalized_argv_tokens: tuple[str, ...]
    typed_target_refs: tuple[object, ...]
    opaque_artifact_refs: tuple[object, ...]
    allowed_next_states: tuple[str, ...]
    proposal_digest: str | None
    intent_payload_digest: str
    trusted_envelope_digest: str
    proof_handle_ref: str | None = None
    proof_handle_digest: str | None = None
    attestation_handle_ref: str | None = None
    attestation_handle_digest: str | None = None

    def as_dict(self) -> dict[str, object]:
        return deepcopy(asdict(self))


@dataclass(frozen=True)
class HumanPresenceProofClaimsV1:
    proof_version: str
    issuer_id: str
    issuer_verification_key_id: str
    audience: str
    local_uid: int
    installation_id: str
    nonce: str
    approval_id: str
    intent_payload_digest: str
    issued_at: object
    expires_at: object
    monotonic_issued_at: object
    monotonic_expires_at: object
    replay_slot: str

    def as_dict(self) -> dict[str, object]:
        return deepcopy(asdict(self))


@dataclass(frozen=True)
class VerifiedHostUserIntentClaimsV1:
    issuer_id: str
    issuer_verification_key_id: str
    audience: str
    authenticated_principal_id: str
    authenticated_session_id: str
    actor_class: str
    input_source: str
    origin_event_id: str
    local_uid: int
    installation_id: str
    nonce: str
    issued_at: object
    expires_at: object
    monotonic_issued_at: object
    monotonic_expires_at: object
    intent_payload_digest: str
    replay_slot: str

    def as_dict(self) -> dict[str, object]:
        return deepcopy(asdict(self))


@dataclass(frozen=True)
class ApprovalReceiptV1:
    approval_id: str
    human_presence_proof_handle_ref: str
    human_presence_issuer_id: str
    human_presence_audience: str
    plan_id: str
    capability_id: str
    job_id: str
    attempt_id: str
    lineage_id: str
    canonical_operation_id: str
    intent_payload_digest: str
    trusted_envelope_digest: str
    normalized_arguments_hash: str
    local_uid: int
    installation_id: str
    signed_manifest_sha256: str
    account_or_tenant_ref: object
    target_ref: object
    target_identity: object
    input_fingerprints: tuple[object, ...]
    record_revisions: dict[str, object]
    nonce: str
    issued_at: object
    expires_at: object
    issuer_channel: str
    state: str = "approved"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["input_fingerprints"] = list(self.input_fingerprints)
        return deepcopy(payload)


@dataclass(frozen=True)
class OperationReceiptV1:
    operation_receipt_id: str
    job_id: str
    attempt_id: str
    lineage_id: str
    installation_id: str
    canonical_operation_id: str
    effect_profile: str
    job_profile: str
    approval_profile: str
    idempotency_key: str
    intent_payload_digest: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class AgentTrustError(Exception):
    """Fail-closed Core trust-boundary error with a stable public code."""

    def __init__(
        self,
        code: str,
        *,
        exit_code: int,
        reason_code: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.reason_code = reason_code
        self.details = {"reason_code": reason_code, **dict(details or {})}


def capability_unavailable(reason_code: str) -> AgentTrustError:
    return AgentTrustError(
        "CAPABILITY_UNAVAILABLE",
        exit_code=30,
        reason_code=reason_code,
    )


def integrity_error(reason_code: str) -> AgentTrustError:
    return AgentTrustError(
        "INTEGRITY_ERROR",
        exit_code=60,
        reason_code=reason_code,
    )
