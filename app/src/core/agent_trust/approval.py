"""Core-owned approval receipt construction and exact proof binding."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Protocol

from .canonical import domain_separated_sha256
from .contracts import (
    ApprovalReceiptV1,
    OperationReceiptV1,
    integrity_error,
)
from .intent import (
    INTENT_PAYLOAD_FIELDS,
    intent_payload_digest,
    trusted_envelope_digest,
)


ApprovalReceipt = ApprovalReceiptV1
OperationReceipt = OperationReceiptV1


class AuthoritativeOperationStore(Protocol):
    """The only allowed source for an existing operation binding."""

    def load_authoritative_binding(self, receipt_id: str) -> dict[str, object]: ...


def _require_nonempty_strings(values: Mapping[str, object]) -> None:
    for field, value in values.items():
        if type(value) is not str or not value:
            raise integrity_error(f"APPROVAL_{field.upper()}_INVALID")


def create_approval_receipt(
    *,
    verified_proof: Mapping[str, object],
    plan_id: str,
    capability_id: str,
    job_id: str,
    attempt_id: str,
    lineage_id: str,
    canonical_operation_id: str,
    trusted_envelope: Mapping[str, object],
    normalized_argv_tokens: list[str],
    signed_manifest_sha256: str,
    account_or_tenant_ref: object,
    target_ref: object,
    target_identity: object,
    input_fingerprints: list[object],
    record_revisions: Mapping[str, object],
    issuer_channel: str,
) -> ApprovalReceipt:
    """Create an immutable receipt exclusively from a verified platform proof."""
    if not isinstance(verified_proof, Mapping):
        raise integrity_error("APPROVAL_PROOF_INVALID")
    if verified_proof.get("issuer_type") != "platform_human_presence_fallback":
        raise integrity_error("APPROVAL_ISSUER_INVALID")
    if verified_proof.get("audience") != "com.lulu.transcribe.core/approval/v1":
        raise integrity_error("APPROVAL_AUDIENCE_INVALID")
    if not isinstance(trusted_envelope, Mapping):
        raise integrity_error("APPROVAL_ENVELOPE_INVALID")
    intent_digest = trusted_envelope.get("intent_payload_digest")
    envelope_digest = trusted_envelope.get("trusted_envelope_digest")
    if verified_proof.get("intent_payload_digest") != intent_digest:
        raise integrity_error("APPROVAL_INTENT_DIGEST_MISMATCH")
    try:
        recomputed_stage1 = intent_payload_digest(
            {field: trusted_envelope[field] for field in INTENT_PAYLOAD_FIELDS}
        )
        stage2_payload = {
            "envelope_version": trusted_envelope["envelope_version"],
            "intent_payload_digest": recomputed_stage1,
            "constructor_mode": trusted_envelope["constructor_mode"],
            "issuer_type": trusted_envelope["issuer_type"],
            "issuer_id": trusted_envelope["issuer_id"],
            "issuer_verification_key_id": trusted_envelope[
                "issuer_verification_key_id"
            ],
            "audience": trusted_envelope["audience"],
            "authenticated_principal_id_or_null": trusted_envelope[
                "authenticated_principal_id"
            ],
            "authenticated_session_id_or_null": trusted_envelope[
                "authenticated_session_id"
            ],
            "actor_class": trusted_envelope["actor_class"],
            "input_source": trusted_envelope["input_source"],
            "local_uid": trusted_envelope["local_uid"],
            "installation_id": trusted_envelope["installation_id"],
            "origin_event_id": trusted_envelope["origin_event_id"],
            "nonce": trusted_envelope["nonce"],
            "issued_at": trusted_envelope["issued_at"],
            "expires_at": trusted_envelope["expires_at"],
            "proposal_digest_or_null": trusted_envelope["proposal_digest"],
            "proof_handle_digest_or_null": trusted_envelope["proof_handle_digest"],
            "attestation_handle_digest_or_null": None,
            "proof_replay_slot_or_null": trusted_envelope["proof_replay_slot"],
            "attestation_replay_slot_or_null": None,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise integrity_error("APPROVAL_TRUSTED_ENVELOPE_INVALID") from exc
    envelope_binding_checks = (
        trusted_envelope.get("constructor_mode")
        == "platform_human_presence_fallback",
        trusted_envelope.get("issuer_type") == "platform_human_presence_fallback",
        trusted_envelope.get("actor_class") == "authenticated_human_user",
        trusted_envelope.get("input_source") == "platform_human_presence_proof",
        trusted_envelope.get("authenticated_principal_id") is None,
        trusted_envelope.get("authenticated_session_id") is None,
        trusted_envelope.get("proof_handle_ref")
        == verified_proof.get("proof_handle_ref"),
        trusted_envelope.get("proof_handle_digest")
        == verified_proof.get("proof_handle_digest"),
        trusted_envelope.get("proof_replay_slot") == verified_proof.get("replay_slot"),
        trusted_envelope.get("issuer_id") == verified_proof.get("issuer_id"),
        trusted_envelope.get("issuer_verification_key_id")
        == verified_proof.get("issuer_verification_key_id"),
        trusted_envelope.get("audience") == verified_proof.get("audience"),
        trusted_envelope.get("local_uid") == verified_proof.get("local_uid"),
        trusted_envelope.get("installation_id")
        == verified_proof.get("installation_id"),
        trusted_envelope.get("nonce") == verified_proof.get("nonce"),
        recomputed_stage1 == intent_digest,
        trusted_envelope_digest(stage2_payload) == envelope_digest,
        trusted_envelope.get("capability_id") == capability_id,
        trusted_envelope.get("command_id") == canonical_operation_id,
        trusted_envelope.get("normalized_argv_tokens") == normalized_argv_tokens,
        target_identity in trusted_envelope.get("typed_target_refs", []),
        trusted_envelope.get("record_and_resource_revisions")
        == dict(record_revisions),
        account_or_tenant_ref
        in (
            trusted_envelope.get("account_ref_or_null"),
            trusted_envelope.get("tenant_ref_or_null"),
        ),
    )
    if not all(envelope_binding_checks):
        raise integrity_error("APPROVAL_TRUSTED_ENVELOPE_INVALID")
    string_values = {
        "approval_id": verified_proof.get("approval_id"),
        "proof_handle_ref": verified_proof.get("proof_handle_ref"),
        "issuer_id": verified_proof.get("issuer_id"),
        "plan_id": plan_id,
        "capability_id": capability_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "lineage_id": lineage_id,
        "canonical_operation_id": canonical_operation_id,
        "intent_payload_digest": intent_digest,
        "trusted_envelope_digest": envelope_digest,
        "installation_id": verified_proof.get("installation_id"),
        "signed_manifest_sha256": signed_manifest_sha256,
        "nonce": verified_proof.get("nonce"),
        "issuer_channel": issuer_channel,
    }
    _require_nonempty_strings(string_values)
    if any(type(token) is not str or not token for token in normalized_argv_tokens):
        raise integrity_error("APPROVAL_NORMALIZED_ARGUMENTS_INVALID")
    if verified_proof.get("local_uid") is None:
        raise integrity_error("APPROVAL_LOCAL_UID_INVALID")
    normalized_arguments_hash = domain_separated_sha256(
        "LULU_NORMALIZED_ARGUMENTS_V1",
        {"normalized_argv_tokens": list(normalized_argv_tokens)},
    )
    return ApprovalReceipt(
        approval_id=string_values["approval_id"],
        human_presence_proof_handle_ref=string_values["proof_handle_ref"],
        human_presence_issuer_id=string_values["issuer_id"],
        human_presence_audience=verified_proof["audience"],
        plan_id=plan_id,
        capability_id=capability_id,
        job_id=job_id,
        attempt_id=attempt_id,
        lineage_id=lineage_id,
        canonical_operation_id=canonical_operation_id,
        intent_payload_digest=string_values["intent_payload_digest"],
        trusted_envelope_digest=string_values["trusted_envelope_digest"],
        normalized_arguments_hash=normalized_arguments_hash,
        local_uid=verified_proof["local_uid"],
        installation_id=string_values["installation_id"],
        signed_manifest_sha256=signed_manifest_sha256,
        account_or_tenant_ref=deepcopy(account_or_tenant_ref),
        target_ref=deepcopy(target_ref),
        target_identity=deepcopy(target_identity),
        input_fingerprints=tuple(deepcopy(input_fingerprints)),
        record_revisions=deepcopy(dict(record_revisions)),
        nonce=string_values["nonce"],
        issued_at=deepcopy(verified_proof["issued_at"]),
        expires_at=deepcopy(verified_proof["expires_at"]),
        issuer_channel=issuer_channel,
    )


def create_operation_receipt(
    *,
    operation_receipt_id: str,
    job_id: str,
    attempt_id: str,
    lineage_id: str,
    installation_id: str,
    canonical_operation_id: str,
    effect_profile: str,
    job_profile: str,
    approval_profile: str,
    idempotency_key: str,
    intent_payload_digest: str,
    created_at: str,
) -> OperationReceipt:
    """Create the immutable authoritative operation identity tuple."""
    values = {
        "operation_receipt_id": operation_receipt_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "lineage_id": lineage_id,
        "installation_id": installation_id,
        "canonical_operation_id": canonical_operation_id,
        "effect_profile": effect_profile,
        "job_profile": job_profile,
        "approval_profile": approval_profile,
        "idempotency_key": idempotency_key,
        "intent_payload_digest": intent_payload_digest,
        "created_at": created_at,
    }
    _require_nonempty_strings(values)
    return OperationReceipt(**values)


def assert_authoritative_operation_binding(
    *,
    store: AuthoritativeOperationStore,
    operation_receipt_id: str,
    assertions: Mapping[str, object],
    current_installation_id: str,
    authoritative_intent_payload_digest: str,
) -> OperationReceipt:
    """Load and compare every binding field before any caller-owned mutation."""
    expected_assertion_fields = frozenset(
        {
            "job_id",
            "expected_attempt_id",
            "expected_lineage_id",
            "expected_installation_id",
            "expected_canonical_operation_id",
            "effect_profile",
            "job_profile",
            "approval_profile",
            "idempotency_key",
        }
    )
    if not isinstance(assertions, Mapping) or frozenset(assertions) != expected_assertion_fields:
        raise integrity_error("OPERATION_ASSERTIONS_INVALID")
    try:
        binding = store.load_authoritative_binding(operation_receipt_id)
    except Exception as exc:  # noqa: BLE001 - authoritative store seam
        raise integrity_error("OPERATION_RECEIPT_UNAVAILABLE") from exc
    if not isinstance(binding, dict) or frozenset(binding) != {"receipt", "job", "attempt"}:
        raise integrity_error("OPERATION_BINDING_INVALID")
    receipt = binding["receipt"]
    job = binding["job"]
    attempt = binding["attempt"]
    if (
        not isinstance(receipt, OperationReceipt)
        or not isinstance(job, dict)
        or not isinstance(attempt, dict)
    ):
        raise integrity_error("OPERATION_BINDING_INVALID")
    immutable = receipt.as_dict()
    comparisons = (
        operation_receipt_id == immutable["operation_receipt_id"],
        assertions["job_id"] == immutable["job_id"] == job.get("job_id"),
        assertions["expected_attempt_id"]
        == immutable["attempt_id"]
        == attempt.get("attempt_id"),
        assertions["expected_lineage_id"]
        == immutable["lineage_id"]
        == attempt.get("lineage_id"),
        assertions["expected_installation_id"]
        == immutable["installation_id"]
        == current_installation_id,
        assertions["expected_canonical_operation_id"]
        == immutable["canonical_operation_id"]
        == attempt.get("canonical_operation_id"),
        assertions["effect_profile"] == immutable["effect_profile"],
        assertions["job_profile"] == immutable["job_profile"],
        assertions["approval_profile"] == immutable["approval_profile"],
        assertions["idempotency_key"] == immutable["idempotency_key"],
        immutable["intent_payload_digest"] == authoritative_intent_payload_digest,
    )
    if not all(comparisons):
        raise integrity_error("OPERATION_BINDING_MISMATCH")
    return receipt
