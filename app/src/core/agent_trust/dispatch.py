"""Final Phase 4 recomputation gate; intentionally contains no business dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib

from .canonical import CanonicalizationError, parse_canonical_json
from .contracts import integrity_error
from .intent import (
    INTENT_PAYLOAD_FIELDS,
    _normalize_typed_request,
    _single_capability,
    _validate_argv,
    _validate_direct_safe_evidence,
    _validate_request_argv_binding,
    construct_core_direct_safe_intent,
    intent_payload_digest,
    trusted_envelope_digest,
)


_DIRECT_SAFE_ENVELOPE_FIELDS = frozenset(
    {
        *INTENT_PAYLOAD_FIELDS,
        "envelope_version",
        "constructor_mode",
        "issuer_type",
        "issuer_id",
        "issuer_verification_key_id",
        "audience",
        "authenticated_principal_id",
        "authenticated_session_id",
        "actor_class",
        "input_source",
        "local_uid",
        "installation_id",
        "origin_event_id",
        "nonce",
        "issued_at",
        "expires_at",
        "allowed_next_states",
        "proposal_digest",
        "intent_payload_digest",
        "trusted_envelope_digest",
    }
)
_VERIFIED_HOST_ENVELOPE_FIELDS = frozenset(
    {
        *_DIRECT_SAFE_ENVELOPE_FIELDS,
        "attestation_handle_ref",
        "attestation_handle_digest",
        "attestation_replay_slot",
    }
)
_PLATFORM_ENVELOPE_FIELDS = frozenset(
    {
        *_DIRECT_SAFE_ENVELOPE_FIELDS,
        "proof_handle_ref",
        "proof_handle_digest",
        "proof_replay_slot",
    }
)

_SUPPORTED_ATTACK_ORACLES = frozenset(
    {
        "ORACLE_ATTACK_FAKE_APPROVAL_RECEIPT",
        "ORACLE_ATTACK_FAKE_HUMAN_ACTION",
        "ORACLE_ATTACK_COMMAND_TARGET_CHANGE",
        "ORACLE_ATTACK_SECRET_REQUEST",
        "ORACLE_ATTACK_IGNORE_CONFIRMATION",
        "ORACLE_ATTACK_FAKE_RETRY_IDEMPOTENCY",
        "ORACLE_ATTACK_JSON_CONTROL_FIELDS",
    }
)


def _recompute_direct_safe(
    envelope: Mapping[str, object], catalog: Mapping[str, object]
) -> None:
    if frozenset(envelope) != _DIRECT_SAFE_ENVELOPE_FIELDS:
        raise integrity_error("TRUSTED_ENVELOPE_FIELD_CLOSURE_FAILED")
    if (
        envelope.get("envelope_version") != "lulu.trusted-intent/v1"
        or envelope.get("constructor_mode") != "core_direct_safe_v1"
        or envelope.get("issuer_type") != "core_direct_safe_constructor"
        or envelope.get("issuer_id") != "lulu-core"
        or envelope.get("issuer_verification_key_id") != "core-direct-safe-v1"
        or envelope.get("audience") != "com.lulu.transcribe.core/intent/v1"
        or envelope.get("authenticated_principal_id") is not None
        or envelope.get("authenticated_session_id") is not None
        or envelope.get("actor_class") != "core_service"
        or envelope.get("input_source") != "core_closed_catalog"
        or envelope.get("allowed_next_states") != ["unavailable"]
        or envelope.get("proposal_digest") is not None
    ):
        raise integrity_error("DIRECT_SAFE_AUTHORITY_MISMATCH")
    capability_id = envelope.get("capability_id")
    if type(capability_id) is not str:
        raise integrity_error("TRUSTED_CAPABILITY_INVALID")
    stage1_payload = {field: envelope[field] for field in INTENT_PAYLOAD_FIELDS}
    recomputed_stage1 = intent_payload_digest(stage1_payload)
    if envelope.get("intent_payload_digest") != recomputed_stage1:
        raise integrity_error("INTENT_PAYLOAD_DIGEST_MISMATCH")
    capability = _single_capability(catalog, capability_id)
    _validate_direct_safe_evidence(catalog, capability)
    expected_profiles = {
        "command_id": capability.get("command_id"),
        "request_schema_profile": capability.get("request_schema_profile"),
        "result_schema_profile": capability.get("result_schema_profile"),
        "effect_profile": capability.get("effect_profile"),
        "approval_profile": capability.get("approval_profile"),
        "lock_profile": capability.get("lock_profile"),
        "job_profile": capability.get("job_profile"),
        "oracle_profile": capability.get("oracle_profile"),
    }
    if any(envelope.get(field) != value for field, value in expected_profiles.items()):
        raise integrity_error("TRUSTED_PROFILE_OR_COMMAND_MISMATCH")
    argv = envelope.get("normalized_argv_tokens")
    if not isinstance(argv, list):
        raise integrity_error("CANONICAL_ARGV_INVALID")
    _validate_argv(capability.get("argv_template"), argv)
    complete_request = envelope.get("typed_request_complete_object")
    if not isinstance(complete_request, Mapping):
        raise integrity_error("TYPED_REQUEST_INVALID")
    reparsed_request = _normalize_typed_request(
        catalog,
        capability.get("request_schema_profile"),
        complete_request,
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), argv, reparsed_request
    )
    if (
        reparsed_request != complete_request
        or envelope.get("typed_request_selector_and_all_dto_fields") != reparsed_request
    ):
        raise integrity_error("TYPED_REQUEST_RECOMPUTATION_MISMATCH")
    stage2_payload = {
        "envelope_version": envelope["envelope_version"],
        "intent_payload_digest": recomputed_stage1,
        "constructor_mode": envelope["constructor_mode"],
        "issuer_type": envelope["issuer_type"],
        "issuer_id": envelope["issuer_id"],
        "issuer_verification_key_id": envelope["issuer_verification_key_id"],
        "audience": envelope["audience"],
        "authenticated_principal_id_or_null": envelope["authenticated_principal_id"],
        "authenticated_session_id_or_null": envelope["authenticated_session_id"],
        "actor_class": envelope["actor_class"],
        "input_source": envelope["input_source"],
        "local_uid": envelope["local_uid"],
        "installation_id": envelope["installation_id"],
        "origin_event_id": envelope["origin_event_id"],
        "nonce": envelope["nonce"],
        "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"],
        "proposal_digest_or_null": None,
        "proof_handle_digest_or_null": None,
        "attestation_handle_digest_or_null": None,
        "proof_replay_slot_or_null": None,
        "attestation_replay_slot_or_null": None,
    }
    if envelope.get("trusted_envelope_digest") != trusted_envelope_digest(stage2_payload):
        raise integrity_error("TRUSTED_ENVELOPE_DIGEST_MISMATCH")


def _recompute_verified_host(
    envelope: Mapping[str, object], catalog: Mapping[str, object]
) -> None:
    if frozenset(envelope) != _VERIFIED_HOST_ENVELOPE_FIELDS:
        raise integrity_error("TRUSTED_ENVELOPE_FIELD_CLOSURE_FAILED")
    if (
        envelope.get("envelope_version") != "lulu.trusted-intent/v1"
        or envelope.get("constructor_mode") != "verified_host_user_intent"
        or envelope.get("issuer_type") != "verified_host_user_intent"
        or envelope.get("audience") != "com.lulu.transcribe.core/intent/v1"
        or type(envelope.get("authenticated_principal_id")) is not str
        or not envelope["authenticated_principal_id"]
        or type(envelope.get("authenticated_session_id")) is not str
        or not envelope["authenticated_session_id"]
        or envelope.get("actor_class") != "authenticated_human_user"
        or envelope.get("input_source") != "host_authenticated_user_instruction"
        or envelope.get("allowed_next_states") != ["unavailable"]
        or type(envelope.get("proposal_digest")) is not str
        or not envelope["proposal_digest"]
    ):
        raise integrity_error("HOST_AUTHORITY_MISMATCH")
    for field in (
        "issuer_id",
        "issuer_verification_key_id",
        "installation_id",
        "origin_event_id",
        "nonce",
        "attestation_handle_ref",
        "attestation_handle_digest",
        "attestation_replay_slot",
    ):
        if type(envelope.get(field)) is not str or not envelope[field]:
            raise integrity_error("HOST_AUTHORITY_MISMATCH")
    stage1_payload = {field: envelope[field] for field in INTENT_PAYLOAD_FIELDS}
    recomputed_stage1 = intent_payload_digest(stage1_payload)
    if envelope.get("intent_payload_digest") != recomputed_stage1:
        raise integrity_error("INTENT_PAYLOAD_DIGEST_MISMATCH")
    capability_id = envelope.get("capability_id")
    if type(capability_id) is not str:
        raise integrity_error("TRUSTED_CAPABILITY_INVALID")
    capability = _single_capability(catalog, capability_id)
    if capability.get("current_cli_state") != "unavailable":
        raise integrity_error("HOST_CAPABILITY_STATE_MISMATCH")
    expected_profiles = {
        "command_id": capability.get("command_id"),
        "request_schema_profile": capability.get("request_schema_profile"),
        "result_schema_profile": capability.get("result_schema_profile"),
        "effect_profile": capability.get("effect_profile"),
        "approval_profile": capability.get("approval_profile"),
        "lock_profile": capability.get("lock_profile"),
        "job_profile": capability.get("job_profile"),
        "oracle_profile": capability.get("oracle_profile"),
    }
    if any(envelope.get(field) != value for field, value in expected_profiles.items()):
        raise integrity_error("TRUSTED_PROFILE_OR_COMMAND_MISMATCH")
    argv = envelope.get("normalized_argv_tokens")
    complete_request = envelope.get("typed_request_complete_object")
    if not isinstance(argv, list) or not isinstance(complete_request, Mapping):
        raise integrity_error("TRUSTED_REQUEST_OR_ARGV_INVALID")
    _validate_argv(capability.get("argv_template"), argv)
    reparsed_request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), complete_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), argv, reparsed_request
    )
    if (
        reparsed_request != complete_request
        or envelope.get("typed_request_selector_and_all_dto_fields") != reparsed_request
    ):
        raise integrity_error("TYPED_REQUEST_RECOMPUTATION_MISMATCH")
    stage2_payload = {
        "envelope_version": envelope["envelope_version"],
        "intent_payload_digest": recomputed_stage1,
        "constructor_mode": envelope["constructor_mode"],
        "issuer_type": envelope["issuer_type"],
        "issuer_id": envelope["issuer_id"],
        "issuer_verification_key_id": envelope["issuer_verification_key_id"],
        "audience": envelope["audience"],
        "authenticated_principal_id_or_null": envelope["authenticated_principal_id"],
        "authenticated_session_id_or_null": envelope["authenticated_session_id"],
        "actor_class": envelope["actor_class"],
        "input_source": envelope["input_source"],
        "local_uid": envelope["local_uid"],
        "installation_id": envelope["installation_id"],
        "origin_event_id": envelope["origin_event_id"],
        "nonce": envelope["nonce"],
        "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"],
        "proposal_digest_or_null": envelope["proposal_digest"],
        "proof_handle_digest_or_null": None,
        "attestation_handle_digest_or_null": envelope["attestation_handle_digest"],
        "proof_replay_slot_or_null": None,
        "attestation_replay_slot_or_null": envelope["attestation_replay_slot"],
    }
    if envelope.get("trusted_envelope_digest") != trusted_envelope_digest(stage2_payload):
        raise integrity_error("TRUSTED_ENVELOPE_DIGEST_MISMATCH")


def _recompute_platform_proof(
    envelope: Mapping[str, object], catalog: Mapping[str, object]
) -> None:
    if frozenset(envelope) != _PLATFORM_ENVELOPE_FIELDS:
        raise integrity_error("TRUSTED_ENVELOPE_FIELD_CLOSURE_FAILED")
    if (
        envelope.get("envelope_version") != "lulu.trusted-intent/v1"
        or envelope.get("constructor_mode") != "platform_human_presence_fallback"
        or envelope.get("issuer_type") != "platform_human_presence_fallback"
        or envelope.get("audience") != "com.lulu.transcribe.core/approval/v1"
        or envelope.get("authenticated_principal_id") is not None
        or envelope.get("authenticated_session_id") is not None
        or envelope.get("actor_class") != "authenticated_human_user"
        or envelope.get("input_source") != "platform_human_presence_proof"
        or envelope.get("allowed_next_states") != ["unavailable"]
        or type(envelope.get("proposal_digest")) is not str
        or not envelope["proposal_digest"]
    ):
        raise integrity_error("PLATFORM_AUTHORITY_MISMATCH")
    for field in (
        "issuer_id",
        "issuer_verification_key_id",
        "installation_id",
        "origin_event_id",
        "nonce",
        "proof_handle_ref",
        "proof_handle_digest",
        "proof_replay_slot",
    ):
        if type(envelope.get(field)) is not str or not envelope[field]:
            raise integrity_error("PLATFORM_AUTHORITY_MISMATCH")
    stage1_payload = {field: envelope[field] for field in INTENT_PAYLOAD_FIELDS}
    recomputed_stage1 = intent_payload_digest(stage1_payload)
    if envelope.get("intent_payload_digest") != recomputed_stage1:
        raise integrity_error("INTENT_PAYLOAD_DIGEST_MISMATCH")
    capability_id = envelope.get("capability_id")
    if type(capability_id) is not str:
        raise integrity_error("TRUSTED_CAPABILITY_INVALID")
    capability = _single_capability(catalog, capability_id)
    if capability.get("current_cli_state") != "unavailable":
        raise integrity_error("PLATFORM_CAPABILITY_STATE_MISMATCH")
    expected_profiles = {
        "command_id": capability.get("command_id"),
        "request_schema_profile": capability.get("request_schema_profile"),
        "result_schema_profile": capability.get("result_schema_profile"),
        "effect_profile": capability.get("effect_profile"),
        "approval_profile": capability.get("approval_profile"),
        "lock_profile": capability.get("lock_profile"),
        "job_profile": capability.get("job_profile"),
        "oracle_profile": capability.get("oracle_profile"),
    }
    if any(envelope.get(field) != value for field, value in expected_profiles.items()):
        raise integrity_error("TRUSTED_PROFILE_OR_COMMAND_MISMATCH")
    argv = envelope.get("normalized_argv_tokens")
    complete_request = envelope.get("typed_request_complete_object")
    if not isinstance(argv, list) or not isinstance(complete_request, Mapping):
        raise integrity_error("TRUSTED_REQUEST_OR_ARGV_INVALID")
    _validate_argv(capability.get("argv_template"), argv)
    reparsed_request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), complete_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), argv, reparsed_request
    )
    if (
        reparsed_request != complete_request
        or envelope.get("typed_request_selector_and_all_dto_fields") != reparsed_request
    ):
        raise integrity_error("TYPED_REQUEST_RECOMPUTATION_MISMATCH")
    stage2_payload = {
        "envelope_version": envelope["envelope_version"],
        "intent_payload_digest": recomputed_stage1,
        "constructor_mode": envelope["constructor_mode"],
        "issuer_type": envelope["issuer_type"],
        "issuer_id": envelope["issuer_id"],
        "issuer_verification_key_id": envelope["issuer_verification_key_id"],
        "audience": envelope["audience"],
        "authenticated_principal_id_or_null": None,
        "authenticated_session_id_or_null": None,
        "actor_class": envelope["actor_class"],
        "input_source": envelope["input_source"],
        "local_uid": envelope["local_uid"],
        "installation_id": envelope["installation_id"],
        "origin_event_id": envelope["origin_event_id"],
        "nonce": envelope["nonce"],
        "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"],
        "proposal_digest_or_null": envelope["proposal_digest"],
        "proof_handle_digest_or_null": envelope["proof_handle_digest"],
        "attestation_handle_digest_or_null": None,
        "proof_replay_slot_or_null": envelope["proof_replay_slot"],
        "attestation_replay_slot_or_null": None,
    }
    if envelope.get("trusted_envelope_digest") != trusted_envelope_digest(stage2_payload):
        raise integrity_error("TRUSTED_ENVELOPE_DIGEST_MISMATCH")


def evaluate_dispatch_gate(
    *,
    envelope: Mapping[str, object],
    catalog: Mapping[str, object],
    business_dispatcher: Callable[[dict[str, object]], object] | None = None,
) -> dict[str, object]:
    """Verify every Core-owned field, then remain unavailable in Phase 4."""
    del business_dispatcher
    if not isinstance(envelope, Mapping) or not isinstance(catalog, Mapping):
        raise integrity_error("TRUSTED_DISPATCH_INPUT_INVALID")
    constructor_mode = envelope.get("constructor_mode")
    if constructor_mode == "core_direct_safe_v1":
        _recompute_direct_safe(envelope, catalog)
    elif constructor_mode == "verified_host_user_intent":
        _recompute_verified_host(envelope, catalog)
    elif constructor_mode == "platform_human_presence_fallback":
        _recompute_platform_proof(envelope, catalog)
    else:
        raise integrity_error("TRUSTED_CONSTRUCTOR_MODE_INVALID")
    return {
        "verified": True,
        "business_dispatch_count": 0,
        "code": "CAPABILITY_UNAVAILABLE",
        "reason_code": "VERIFIED_BUT_CAPABILITY_UNAVAILABLE",
        "exit_code": 30,
    }


def evaluate_attack_oracle(
    *,
    raw_payload: bytes,
    catalog: Mapping[str, object],
    proof_replay_store,
    attestation_replay_store,
    trusted_intent_queue,
    trusted_intent_cache,
    approval_ledger,
    job_ledger,
    operation_receipt_store,
    effect_spy,
) -> dict[str, object]:
    """Parse an attack as data, verify a real intent, and prove runtime state stayed frozen."""
    try:
        payload = parse_canonical_json(raw_payload)
    except CanonicalizationError as exc:
        raise integrity_error("ATTACK_PAYLOAD_INVALID") from exc
    if not isinstance(payload, dict) or frozenset(payload) != {
        "oracle_profile_id",
        "untrusted_data_plane",
        "intent_request",
    }:
        raise integrity_error("ATTACK_PAYLOAD_INVALID")
    oracle_profile_id = payload["oracle_profile_id"]
    if oracle_profile_id not in _SUPPORTED_ATTACK_ORACLES:
        raise integrity_error("ATTACK_ORACLE_UNSUPPORTED")
    oracle_profiles = catalog.get("oracle_profiles")
    if not isinstance(oracle_profiles, dict):
        raise integrity_error("ATTACK_ORACLE_CATALOG_INVALID")
    base_profile = oracle_profiles.get("BASE_AUTHORITY_PROVENANCE_REPLAY_ORACLE")
    profile = oracle_profiles.get(oracle_profile_id)
    if not isinstance(base_profile, dict) or not isinstance(profile, dict):
        raise integrity_error("ATTACK_ORACLE_CATALOG_INVALID")
    if (
        base_profile.get("kind") != "base_security_oracle"
        or profile.get("inherits") != "BASE_AUTHORITY_PROVENANCE_REPLAY_ORACLE"
        or profile.get("strict_superset") is not True
    ):
        raise integrity_error("ATTACK_ORACLE_INHERITANCE_INVALID")
    additional_fields = profile.get("additional_compare")
    if (
        not isinstance(additional_fields, list)
        or not additional_fields
        or any(type(field) is not str for field in additional_fields)
    ):
        raise integrity_error("ATTACK_ORACLE_FIELDS_INVALID")

    data_plane = payload["untrusted_data_plane"]
    if not isinstance(data_plane, dict) or frozenset(data_plane) != {
        "content",
        "artifact_ref",
    }:
        raise integrity_error("ATTACK_DATA_PLANE_INVALID")
    content = data_plane["content"]
    artifact_ref = data_plane["artifact_ref"]
    if type(content) is not str or not isinstance(artifact_ref, dict):
        raise integrity_error("ATTACK_DATA_PLANE_INVALID")
    if frozenset(artifact_ref) != {"artifact_id", "kind", "sha256"} or (
        type(artifact_ref.get("artifact_id")) is not str
        or not artifact_ref["artifact_id"]
        or artifact_ref.get("kind") != "untrusted_text"
        or artifact_ref.get("sha256")
        != hashlib.sha256(content.encode("utf-8")).hexdigest()
    ):
        raise integrity_error("ATTACK_ARTIFACT_BINDING_INVALID")

    request = payload["intent_request"]
    request_fields = {
        "capability_id",
        "typed_request",
        "normalized_argv_tokens",
        "typed_target_refs",
        "account_ref",
        "tenant_ref",
        "record_and_resource_revisions",
        "idempotency_key",
        "local_uid",
        "installation_id",
        "origin_event_id",
        "nonce",
        "issued_at",
        "expires_at",
    }
    if not isinstance(request, dict) or frozenset(request) != request_fields:
        raise integrity_error("ATTACK_INTENT_REQUEST_INVALID")

    runtime_components = {
        "proof_replay_store": proof_replay_store,
        "attestation_replay_store": attestation_replay_store,
        "trusted_intent_queue": trusted_intent_queue,
        "trusted_intent_cache": trusted_intent_cache,
        "job_ledger": job_ledger,
        "operation_receipt_store": operation_receipt_store,
        "effect_spy": effect_spy,
    }

    def snapshot_component(component):
        snapshot = getattr(component, "snapshot", None)
        mutation_count = getattr(component, "mutation_count", None)
        if not callable(snapshot) or type(mutation_count) is not int:
            raise integrity_error("ATTACK_RUNTIME_SPY_INVALID")
        return snapshot(), mutation_count

    def snapshot_ledger():
        try:
            return approval_ledger.head, approval_ledger.journal_path.stat().st_size
        except (AttributeError, OSError) as exc:
            raise integrity_error("ATTACK_LEDGER_SPY_INVALID") from exc

    before = {name: snapshot_component(spy) for name, spy in runtime_components.items()}
    before_ledger = snapshot_ledger()
    if any(mutation_count != 0 for _state, mutation_count in before.values()):
        raise integrity_error("ATTACK_RUNTIME_SPY_NOT_CLEAN")

    envelope = construct_core_direct_safe_intent(
        catalog=catalog,
        capability_id=request["capability_id"],
        typed_request=request["typed_request"],
        normalized_argv_tokens=request["normalized_argv_tokens"],
        typed_target_refs=request["typed_target_refs"],
        opaque_artifact_refs=[dict(artifact_ref)],
        account_ref=request["account_ref"],
        tenant_ref=request["tenant_ref"],
        record_and_resource_revisions=request["record_and_resource_revisions"],
        idempotency_key=request["idempotency_key"],
        local_uid=request["local_uid"],
        installation_id=request["installation_id"],
        origin_event_id=request["origin_event_id"],
        nonce=request["nonce"],
        issued_at=request["issued_at"],
        expires_at=request["expires_at"],
    )
    dispatch_result = evaluate_dispatch_gate(envelope=envelope, catalog=catalog)

    after = {name: snapshot_component(spy) for name, spy in runtime_components.items()}
    after_ledger = snapshot_ledger()
    if before != after or before_ledger != after_ledger:
        raise integrity_error("ATTACK_RUNTIME_MUTATION_DETECTED")

    return {
        "status": "discarded_untrusted_instruction",
        "oracle_profile_id": oracle_profile_id,
        "base_oracle_profile_id": "BASE_AUTHORITY_PROVENANCE_REPLAY_ORACLE",
        "strict_superset": True,
        "runtime_path": [
            "canonical_parser",
            "intent_constructor",
            "dispatch_gate",
            "mutation_guards",
        ],
        "zero_mutation_components": list(runtime_components) + ["approval_ledger"],
        "artifact_preserved": True,
        "control_fields_accepted": 0,
        "business_dispatch_count": dispatch_result["business_dispatch_count"],
    }
