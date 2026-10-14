"""Closed Phase 4 intent-payload digest construction."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .canonical import CanonicalizationError, domain_separated_sha256
from .contracts import capability_unavailable, integrity_error


INTENT_PAYLOAD_DOMAIN = "LULU_INTENT_PAYLOAD_V1"
TRUSTED_ENVELOPE_DOMAIN = "LULU_TRUSTED_ENVELOPE_V1"
INTENT_PAYLOAD_FIELDS = (
    "typed_request_complete_object",
    "typed_request_selector_and_all_dto_fields",
    "capability_id",
    "command_id",
    "normalized_argv_tokens",
    "typed_target_refs",
    "opaque_artifact_refs",
    "account_ref_or_null",
    "tenant_ref_or_null",
    "record_and_resource_revisions",
    "idempotency_key_or_null",
    "request_schema_profile",
    "result_schema_profile",
    "effect_profile",
    "approval_profile",
    "lock_profile",
    "job_profile",
    "oracle_profile",
)
TRUSTED_ENVELOPE_FIELDS = (
    "envelope_version",
    "intent_payload_digest",
    "constructor_mode",
    "issuer_type",
    "issuer_id",
    "issuer_verification_key_id",
    "audience",
    "authenticated_principal_id_or_null",
    "authenticated_session_id_or_null",
    "actor_class",
    "input_source",
    "local_uid",
    "installation_id",
    "origin_event_id",
    "nonce",
    "issued_at",
    "expires_at",
    "proposal_digest_or_null",
    "proof_handle_digest_or_null",
    "attestation_handle_digest_or_null",
    "proof_replay_slot_or_null",
    "attestation_replay_slot_or_null",
)

DIRECT_SAFE_CAPABILITY_IDS = frozenset(
    {
        "SYS-01",
        "SYS-02",
        "SYS-03",
        "SYS-04",
        "SYS-05",
        "AUTH-01",
        "TR-01",
        "COL-01",
        "COL-06",
        "COL-11",
        "LIB-01",
        "LIB-02",
        "COPY-01",
        "COPY-07",
        "VOI-01",
        "TTS-01",
        "MOD-01",
        "MOD-02",
        "CFG-01",
        "CFG-04",
        "INT-01",
        "INT-02",
        "INT-04",
        "UPD-01",
    }
)

_DIRECT_SAFE_EFFECT = {
    "network": False,
    "uploads_content": False,
    "writes_disk": False,
    "may_charge": False,
    "requires_human_approval": False,
}


def intent_payload_digest(payload: Mapping[str, object]) -> str:
    """Return stage 1 only after every contract field is present and no extra exists."""
    if not isinstance(payload, Mapping):
        raise CanonicalizationError("intent payload must be an object")
    expected = frozenset(INTENT_PAYLOAD_FIELDS)
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise CanonicalizationError(
            f"intent payload field closure failed: missing={missing}, unknown={unknown}"
        )
    closed_payload = {field: payload[field] for field in INTENT_PAYLOAD_FIELDS}
    return domain_separated_sha256(INTENT_PAYLOAD_DOMAIN, closed_payload)


def trusted_envelope_digest(payload: Mapping[str, object]) -> str:
    """Return stage 2 over verified authority claims while excluding itself."""
    if not isinstance(payload, Mapping):
        raise CanonicalizationError("trusted envelope payload must be an object")
    expected = frozenset(TRUSTED_ENVELOPE_FIELDS)
    actual = frozenset(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise CanonicalizationError(
            f"trusted envelope field closure failed: missing={missing}, unknown={unknown}"
        )
    closed_payload = {field: payload[field] for field in TRUSTED_ENVELOPE_FIELDS}
    return domain_separated_sha256(TRUSTED_ENVELOPE_DOMAIN, closed_payload)


def _single_capability(catalog: Mapping[str, object], capability_id: str) -> dict[str, object]:
    capabilities = catalog.get("capabilities")
    if not isinstance(capabilities, list):
        raise capability_unavailable("DIRECT_SAFE_CATALOG_AMBIGUOUS")
    matches = [
        item
        for item in capabilities
        if isinstance(item, dict) and item.get("capability_id") == capability_id
    ]
    if len(matches) != 1:
        raise capability_unavailable("DIRECT_SAFE_CAPABILITY_AMBIGUOUS")
    return matches[0]


def _registry_entry(
    catalog: Mapping[str, object], registry_name: str, profile_id: object
) -> dict[str, object]:
    registry = catalog.get(registry_name)
    if not isinstance(profile_id, str) or not isinstance(registry, dict):
        raise capability_unavailable("DIRECT_SAFE_PROFILE_AMBIGUOUS")
    profile = registry.get(profile_id)
    if not isinstance(profile, dict):
        raise capability_unavailable("DIRECT_SAFE_PROFILE_AMBIGUOUS")
    return profile


def _validate_direct_safe_evidence(
    catalog: Mapping[str, object], capability: Mapping[str, object]
) -> None:
    if capability.get("capability_id") not in DIRECT_SAFE_CAPABILITY_IDS:
        raise capability_unavailable("DIRECT_SAFE_NOT_ALLOWLISTED")
    if capability.get("classification") != "Agent 直调":
        raise capability_unavailable("DIRECT_SAFE_CLASSIFICATION_MISMATCH")
    if capability.get("current_cli_state") != "unavailable":
        raise capability_unavailable("DIRECT_SAFE_CURRENT_STATE_MISMATCH")
    if capability.get("effect_profile") != "EFFECT_LOCAL_READ":
        raise capability_unavailable("DIRECT_SAFE_EFFECT_MISMATCH")
    if capability.get("approval_profile") != "APPROVAL_NONE":
        raise capability_unavailable("DIRECT_SAFE_APPROVAL_MISMATCH")
    if capability.get("job_profile") != "JOB_NONE":
        raise capability_unavailable("DIRECT_SAFE_JOB_MISMATCH")
    effect = _registry_entry(catalog, "effect_profiles", "EFFECT_LOCAL_READ")
    approval = _registry_entry(catalog, "approval_profiles", "APPROVAL_NONE")
    job = _registry_entry(catalog, "job_profiles", "JOB_NONE")
    if effect != _DIRECT_SAFE_EFFECT:
        raise capability_unavailable("DIRECT_SAFE_EFFECT_AMBIGUOUS")
    if approval != {"required": False}:
        raise capability_unavailable("DIRECT_SAFE_APPROVAL_AMBIGUOUS")
    if job.get("persistent") is not False:
        raise capability_unavailable("DIRECT_SAFE_JOB_AMBIGUOUS")


def _validate_argv(template: object, tokens: list[str]) -> None:
    if not isinstance(template, str) or not tokens or any(type(item) is not str for item in tokens):
        raise integrity_error("CANONICAL_ARGV_INVALID")
    expected_tokens = template.split()
    if len(expected_tokens) != len(tokens):
        raise integrity_error("CANONICAL_ARGV_MISMATCH")
    for expected, actual in zip(expected_tokens, tokens):
        if expected.startswith("<") and expected.endswith(">"):
            if not actual or actual.startswith("--"):
                raise integrity_error("CANONICAL_ARGV_MISMATCH")
            continue
        if expected.startswith("{") and expected.endswith("}"):
            if actual not in expected[1:-1].split("|"):
                raise integrity_error("CANONICAL_ARGV_MISMATCH")
            continue
        if "|" in expected:
            if actual not in expected.split("|"):
                raise integrity_error("CANONICAL_ARGV_MISMATCH")
            continue
        if expected != actual:
            raise integrity_error("CANONICAL_ARGV_MISMATCH")


def _validate_request_argv_binding(
    template: object,
    tokens: list[str],
    request: Mapping[str, object],
) -> None:
    if not isinstance(template, str):
        raise integrity_error("CANONICAL_ARGV_INVALID")
    template_tokens = template.split()
    placeholder_fields = {
        "file": "source",
        "directory": "destination",
        "json_ref": "source_ref",
    }
    for index, (expected, actual) in enumerate(zip(template_tokens, tokens)):
        if expected.startswith("<") and expected.endswith(">"):
            placeholder = expected[1:-1].replace("-", "_")
            field = placeholder_fields.get(placeholder, placeholder)
            if field in request and request[field] != actual:
                raise integrity_error("TYPED_REQUEST_ARGV_BINDING_MISMATCH")
        elif "|" in expected and not expected.startswith("--"):
            previous = template_tokens[index - 1] if index else ""
            if not previous.startswith("--") and "operation" in request:
                if request["operation"] != actual:
                    raise integrity_error("TYPED_REQUEST_ARGV_BINDING_MISMATCH")
        elif expected.startswith("{") and expected.endswith("}"):
            previous = template_tokens[index - 1] if index else ""
            if previous.startswith("--"):
                field = previous[2:].replace("-", "_")
                if field in request and request[field] != actual:
                    raise integrity_error("TYPED_REQUEST_ARGV_BINDING_MISMATCH")


def _normalize_typed_request(
    catalog: Mapping[str, object], profile_id: object, typed_request: Mapping[str, object]
) -> dict[str, object]:
    profile = _registry_entry(catalog, "request_schema_profiles", profile_id)
    if not isinstance(typed_request, Mapping):
        raise integrity_error("TYPED_REQUEST_INVALID")
    required = profile.get("required")
    optional = profile.get("optional")
    defaults = profile.get("defaults")
    if not isinstance(required, list) or not isinstance(optional, list) or not isinstance(defaults, dict):
        raise capability_unavailable("DIRECT_SAFE_REQUEST_SCHEMA_AMBIGUOUS")
    allowed = set(required) | set(optional)
    if any(type(field) is not str for field in allowed):
        raise capability_unavailable("DIRECT_SAFE_REQUEST_SCHEMA_AMBIGUOUS")
    unknown = sorted(set(typed_request) - allowed)
    missing = sorted(set(required) - set(typed_request))
    if unknown:
        raise integrity_error("TYPED_REQUEST_UNKNOWN_FIELD")
    if missing:
        raise integrity_error("TYPED_REQUEST_REQUIRED_FIELD_MISSING")
    normalized = deepcopy(dict(typed_request))
    for field, value in defaults.items():
        normalized.setdefault(field, deepcopy(value))
    return normalized


def construct_core_direct_safe_intent(
    *,
    catalog: Mapping[str, object],
    capability_id: str,
    typed_request: Mapping[str, object],
    normalized_argv_tokens: list[str],
    typed_target_refs: list[object],
    opaque_artifact_refs: list[object],
    account_ref: object,
    tenant_ref: object,
    record_and_resource_revisions: Mapping[str, object],
    idempotency_key: object,
    local_uid: int,
    installation_id: str,
    origin_event_id: str,
    nonce: str,
    issued_at: str,
    expires_at: str,
) -> dict[str, object]:
    """Construct a Core-owned envelope for the exact direct-safe subset only."""
    capability = _single_capability(catalog, capability_id)
    _validate_direct_safe_evidence(catalog, capability)
    _validate_argv(capability.get("argv_template"), normalized_argv_tokens)
    request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), typed_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), normalized_argv_tokens, request
    )
    stage1_payload = {
        "typed_request_complete_object": request,
        "typed_request_selector_and_all_dto_fields": deepcopy(request),
        "capability_id": capability_id,
        "command_id": capability["command_id"],
        "normalized_argv_tokens": list(normalized_argv_tokens),
        "typed_target_refs": deepcopy(typed_target_refs),
        "opaque_artifact_refs": deepcopy(opaque_artifact_refs),
        "account_ref_or_null": deepcopy(account_ref),
        "tenant_ref_or_null": deepcopy(tenant_ref),
        "record_and_resource_revisions": deepcopy(dict(record_and_resource_revisions)),
        "idempotency_key_or_null": deepcopy(idempotency_key),
        "request_schema_profile": capability["request_schema_profile"],
        "result_schema_profile": capability["result_schema_profile"],
        "effect_profile": capability["effect_profile"],
        "approval_profile": capability["approval_profile"],
        "lock_profile": capability["lock_profile"],
        "job_profile": capability["job_profile"],
        "oracle_profile": capability["oracle_profile"],
    }
    stage1 = intent_payload_digest(stage1_payload)
    stage2_payload = {
        "envelope_version": "lulu.trusted-intent/v1",
        "intent_payload_digest": stage1,
        "constructor_mode": "core_direct_safe_v1",
        "issuer_type": "core_direct_safe_constructor",
        "issuer_id": "lulu-core",
        "issuer_verification_key_id": "core-direct-safe-v1",
        "audience": "com.lulu.transcribe.core/intent/v1",
        "authenticated_principal_id_or_null": None,
        "authenticated_session_id_or_null": None,
        "actor_class": "core_service",
        "input_source": "core_closed_catalog",
        "local_uid": local_uid,
        "installation_id": installation_id,
        "origin_event_id": origin_event_id,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "proposal_digest_or_null": None,
        "proof_handle_digest_or_null": None,
        "attestation_handle_digest_or_null": None,
        "proof_replay_slot_or_null": None,
        "attestation_replay_slot_or_null": None,
    }
    return {
        **stage1_payload,
        **{
            "envelope_version": stage2_payload["envelope_version"],
            "constructor_mode": stage2_payload["constructor_mode"],
            "issuer_type": stage2_payload["issuer_type"],
            "issuer_id": stage2_payload["issuer_id"],
            "issuer_verification_key_id": stage2_payload["issuer_verification_key_id"],
            "audience": stage2_payload["audience"],
            "authenticated_principal_id": None,
            "authenticated_session_id": None,
            "actor_class": stage2_payload["actor_class"],
            "input_source": stage2_payload["input_source"],
            "local_uid": local_uid,
            "installation_id": installation_id,
            "origin_event_id": origin_event_id,
            "nonce": nonce,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "allowed_next_states": ["unavailable"],
            "proposal_digest": None,
            "intent_payload_digest": stage1,
            "trusted_envelope_digest": trusted_envelope_digest(stage2_payload),
        },
    }


def prepare_intent_proposal(
    *,
    catalog: Mapping[str, object],
    producer: str,
    plan_id: str,
    capability_id: str,
    typed_request: Mapping[str, object],
    normalized_argv_tokens: list[str],
    typed_target_refs: list[object],
    opaque_artifact_refs: list[object],
    account_ref: object,
    tenant_ref: object,
    record_and_resource_revisions: Mapping[str, object],
    idempotency_key: object,
) -> dict[str, object]:
    """Create a preview-only proposal with no authority or state-transition fields."""
    if producer not in {"Skill", "Agent"} or type(plan_id) is not str or not plan_id:
        raise integrity_error("INTENT_PROPOSAL_IDENTITY_INVALID")
    capability = _single_capability(catalog, capability_id)
    _validate_argv(capability.get("argv_template"), normalized_argv_tokens)
    request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), typed_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), normalized_argv_tokens, request
    )
    for registry_name, field in (
        ("result_schema_profiles", "result_schema_profile"),
        ("effect_profiles", "effect_profile"),
        ("approval_profiles", "approval_profile"),
        ("lock_profiles", "lock_profile"),
        ("job_profiles", "job_profile"),
        ("oracle_profiles", "oracle_profile"),
    ):
        _registry_entry(catalog, registry_name, capability.get(field))
    stage1_payload = {
        "typed_request_complete_object": request,
        "typed_request_selector_and_all_dto_fields": deepcopy(request),
        "capability_id": capability_id,
        "command_id": capability["command_id"],
        "normalized_argv_tokens": list(normalized_argv_tokens),
        "typed_target_refs": deepcopy(typed_target_refs),
        "opaque_artifact_refs": deepcopy(opaque_artifact_refs),
        "account_ref_or_null": deepcopy(account_ref),
        "tenant_ref_or_null": deepcopy(tenant_ref),
        "record_and_resource_revisions": deepcopy(dict(record_and_resource_revisions)),
        "idempotency_key_or_null": deepcopy(idempotency_key),
        "request_schema_profile": capability["request_schema_profile"],
        "result_schema_profile": capability["result_schema_profile"],
        "effect_profile": capability["effect_profile"],
        "approval_profile": capability["approval_profile"],
        "lock_profile": capability["lock_profile"],
        "job_profile": capability["job_profile"],
        "oracle_profile": capability["oracle_profile"],
    }
    proposal = {
        "proposal_version": "lulu.intent-proposal/v1",
        "producer": producer,
        "trust_level": "untrusted_data_plane",
        "plan_id": plan_id,
        **stage1_payload,
        "current_cli_state": capability["current_cli_state"],
        "approved": False,
        "dispatchable": False,
        "intent_payload_digest": intent_payload_digest(stage1_payload),
    }
    return {
        **proposal,
        "proposal_digest": domain_separated_sha256(
            "LULU_INTENT_PROPOSAL_V1", proposal
        ),
    }


def construct_verified_host_intent(
    *,
    catalog: Mapping[str, object],
    capability_id: str,
    typed_request: Mapping[str, object],
    normalized_argv_tokens: list[str],
    typed_target_refs: list[object],
    opaque_artifact_refs: list[object],
    account_ref: object,
    tenant_ref: object,
    record_and_resource_revisions: Mapping[str, object],
    idempotency_key: object,
    proposal_digest: str,
    verified_authority: Mapping[str, object],
) -> dict[str, object]:
    """Construct stage 2 only from already verified human host provenance."""
    if type(proposal_digest) is not str or not proposal_digest:
        raise integrity_error("HOST_PROPOSAL_DIGEST_INVALID")
    if not isinstance(verified_authority, Mapping):
        raise integrity_error("HOST_AUTHORITY_INVALID")
    if (
        verified_authority.get("issuer_type") != "verified_host_user_intent"
        or verified_authority.get("actor_class") != "authenticated_human_user"
        or verified_authority.get("input_source")
        != "host_authenticated_user_instruction"
    ):
        raise integrity_error("HOST_AUTHORITY_INVALID")
    for field in (
        "issuer_id",
        "issuer_verification_key_id",
        "authenticated_principal_id",
        "authenticated_session_id",
        "origin_event_id",
        "installation_id",
        "nonce",
        "attestation_handle_ref",
        "attestation_handle_digest",
        "replay_slot",
    ):
        if type(verified_authority.get(field)) is not str or not verified_authority[field]:
            raise integrity_error("HOST_AUTHORITY_INCOMPLETE")
    capability = _single_capability(catalog, capability_id)
    if capability.get("current_cli_state") != "unavailable":
        raise capability_unavailable("HOST_CAPABILITY_NOT_UNAVAILABLE")
    _validate_argv(capability.get("argv_template"), normalized_argv_tokens)
    request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), typed_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), normalized_argv_tokens, request
    )
    for registry_name, field in (
        ("result_schema_profiles", "result_schema_profile"),
        ("effect_profiles", "effect_profile"),
        ("approval_profiles", "approval_profile"),
        ("lock_profiles", "lock_profile"),
        ("job_profiles", "job_profile"),
        ("oracle_profiles", "oracle_profile"),
    ):
        _registry_entry(catalog, registry_name, capability.get(field))
    stage1_payload = {
        "typed_request_complete_object": request,
        "typed_request_selector_and_all_dto_fields": deepcopy(request),
        "capability_id": capability_id,
        "command_id": capability["command_id"],
        "normalized_argv_tokens": list(normalized_argv_tokens),
        "typed_target_refs": deepcopy(typed_target_refs),
        "opaque_artifact_refs": deepcopy(opaque_artifact_refs),
        "account_ref_or_null": deepcopy(account_ref),
        "tenant_ref_or_null": deepcopy(tenant_ref),
        "record_and_resource_revisions": deepcopy(dict(record_and_resource_revisions)),
        "idempotency_key_or_null": deepcopy(idempotency_key),
        "request_schema_profile": capability["request_schema_profile"],
        "result_schema_profile": capability["result_schema_profile"],
        "effect_profile": capability["effect_profile"],
        "approval_profile": capability["approval_profile"],
        "lock_profile": capability["lock_profile"],
        "job_profile": capability["job_profile"],
        "oracle_profile": capability["oracle_profile"],
    }
    stage1 = intent_payload_digest(stage1_payload)
    if verified_authority.get("intent_payload_digest") != stage1:
        raise integrity_error("HOST_INTENT_DIGEST_MISMATCH")
    stage2_payload = {
        "envelope_version": "lulu.trusted-intent/v1",
        "intent_payload_digest": stage1,
        "constructor_mode": "verified_host_user_intent",
        "issuer_type": verified_authority["issuer_type"],
        "issuer_id": verified_authority["issuer_id"],
        "issuer_verification_key_id": verified_authority["issuer_verification_key_id"],
        "audience": verified_authority["audience"],
        "authenticated_principal_id_or_null": verified_authority[
            "authenticated_principal_id"
        ],
        "authenticated_session_id_or_null": verified_authority[
            "authenticated_session_id"
        ],
        "actor_class": verified_authority["actor_class"],
        "input_source": verified_authority["input_source"],
        "local_uid": verified_authority["local_uid"],
        "installation_id": verified_authority["installation_id"],
        "origin_event_id": verified_authority["origin_event_id"],
        "nonce": verified_authority["nonce"],
        "issued_at": verified_authority["issued_at"],
        "expires_at": verified_authority["expires_at"],
        "proposal_digest_or_null": proposal_digest,
        "proof_handle_digest_or_null": None,
        "attestation_handle_digest_or_null": verified_authority[
            "attestation_handle_digest"
        ],
        "proof_replay_slot_or_null": None,
        "attestation_replay_slot_or_null": verified_authority["replay_slot"],
    }
    return {
        **stage1_payload,
        "envelope_version": "lulu.trusted-intent/v1",
        "constructor_mode": "verified_host_user_intent",
        "issuer_type": verified_authority["issuer_type"],
        "issuer_id": verified_authority["issuer_id"],
        "issuer_verification_key_id": verified_authority["issuer_verification_key_id"],
        "audience": verified_authority["audience"],
        "authenticated_principal_id": verified_authority[
            "authenticated_principal_id"
        ],
        "authenticated_session_id": verified_authority["authenticated_session_id"],
        "actor_class": verified_authority["actor_class"],
        "input_source": verified_authority["input_source"],
        "local_uid": verified_authority["local_uid"],
        "installation_id": verified_authority["installation_id"],
        "origin_event_id": verified_authority["origin_event_id"],
        "nonce": verified_authority["nonce"],
        "issued_at": verified_authority["issued_at"],
        "expires_at": verified_authority["expires_at"],
        "attestation_handle_ref": verified_authority["attestation_handle_ref"],
        "attestation_handle_digest": verified_authority["attestation_handle_digest"],
        "attestation_replay_slot": verified_authority["replay_slot"],
        "allowed_next_states": ["unavailable"],
        "proposal_digest": proposal_digest,
        "intent_payload_digest": stage1,
        "trusted_envelope_digest": trusted_envelope_digest(stage2_payload),
    }


def construct_platform_human_presence_intent(
    *,
    catalog: Mapping[str, object],
    capability_id: str,
    typed_request: Mapping[str, object],
    normalized_argv_tokens: list[str],
    typed_target_refs: list[object],
    opaque_artifact_refs: list[object],
    account_ref: object,
    tenant_ref: object,
    record_and_resource_revisions: Mapping[str, object],
    idempotency_key: object,
    proposal_digest: str,
    verified_proof: Mapping[str, object],
) -> dict[str, object]:
    """Construct stage 2 from a verified platform proof without consuming it."""
    if type(proposal_digest) is not str or not proposal_digest:
        raise integrity_error("PLATFORM_PROPOSAL_DIGEST_INVALID")
    if not isinstance(verified_proof, Mapping) or verified_proof.get(
        "issuer_type"
    ) != "platform_human_presence_fallback":
        raise integrity_error("PLATFORM_AUTHORITY_INVALID")
    for field in (
        "issuer_id",
        "issuer_verification_key_id",
        "installation_id",
        "nonce",
        "approval_id",
        "proof_handle_ref",
        "proof_handle_digest",
        "replay_slot",
    ):
        if type(verified_proof.get(field)) is not str or not verified_proof[field]:
            raise integrity_error("PLATFORM_AUTHORITY_INCOMPLETE")
    capability = _single_capability(catalog, capability_id)
    if capability.get("current_cli_state") != "unavailable":
        raise capability_unavailable("PLATFORM_CAPABILITY_NOT_UNAVAILABLE")
    _validate_argv(capability.get("argv_template"), normalized_argv_tokens)
    request = _normalize_typed_request(
        catalog, capability.get("request_schema_profile"), typed_request
    )
    _validate_request_argv_binding(
        capability.get("argv_template"), normalized_argv_tokens, request
    )
    for registry_name, field in (
        ("result_schema_profiles", "result_schema_profile"),
        ("effect_profiles", "effect_profile"),
        ("approval_profiles", "approval_profile"),
        ("lock_profiles", "lock_profile"),
        ("job_profiles", "job_profile"),
        ("oracle_profiles", "oracle_profile"),
    ):
        _registry_entry(catalog, registry_name, capability.get(field))
    stage1_payload = {
        "typed_request_complete_object": request,
        "typed_request_selector_and_all_dto_fields": deepcopy(request),
        "capability_id": capability_id,
        "command_id": capability["command_id"],
        "normalized_argv_tokens": list(normalized_argv_tokens),
        "typed_target_refs": deepcopy(typed_target_refs),
        "opaque_artifact_refs": deepcopy(opaque_artifact_refs),
        "account_ref_or_null": deepcopy(account_ref),
        "tenant_ref_or_null": deepcopy(tenant_ref),
        "record_and_resource_revisions": deepcopy(dict(record_and_resource_revisions)),
        "idempotency_key_or_null": deepcopy(idempotency_key),
        "request_schema_profile": capability["request_schema_profile"],
        "result_schema_profile": capability["result_schema_profile"],
        "effect_profile": capability["effect_profile"],
        "approval_profile": capability["approval_profile"],
        "lock_profile": capability["lock_profile"],
        "job_profile": capability["job_profile"],
        "oracle_profile": capability["oracle_profile"],
    }
    stage1 = intent_payload_digest(stage1_payload)
    if verified_proof.get("intent_payload_digest") != stage1:
        raise integrity_error("PLATFORM_INTENT_DIGEST_MISMATCH")
    origin_event_id = f"approval:{verified_proof['approval_id']}"
    stage2_payload = {
        "envelope_version": "lulu.trusted-intent/v1",
        "intent_payload_digest": stage1,
        "constructor_mode": "platform_human_presence_fallback",
        "issuer_type": verified_proof["issuer_type"],
        "issuer_id": verified_proof["issuer_id"],
        "issuer_verification_key_id": verified_proof["issuer_verification_key_id"],
        "audience": verified_proof["audience"],
        "authenticated_principal_id_or_null": None,
        "authenticated_session_id_or_null": None,
        "actor_class": "authenticated_human_user",
        "input_source": "platform_human_presence_proof",
        "local_uid": verified_proof["local_uid"],
        "installation_id": verified_proof["installation_id"],
        "origin_event_id": origin_event_id,
        "nonce": verified_proof["nonce"],
        "issued_at": verified_proof["issued_at"],
        "expires_at": verified_proof["expires_at"],
        "proposal_digest_or_null": proposal_digest,
        "proof_handle_digest_or_null": verified_proof["proof_handle_digest"],
        "attestation_handle_digest_or_null": None,
        "proof_replay_slot_or_null": verified_proof["replay_slot"],
        "attestation_replay_slot_or_null": None,
    }
    return {
        **stage1_payload,
        "envelope_version": "lulu.trusted-intent/v1",
        "constructor_mode": "platform_human_presence_fallback",
        "issuer_type": verified_proof["issuer_type"],
        "issuer_id": verified_proof["issuer_id"],
        "issuer_verification_key_id": verified_proof["issuer_verification_key_id"],
        "audience": verified_proof["audience"],
        "authenticated_principal_id": None,
        "authenticated_session_id": None,
        "actor_class": "authenticated_human_user",
        "input_source": "platform_human_presence_proof",
        "local_uid": verified_proof["local_uid"],
        "installation_id": verified_proof["installation_id"],
        "origin_event_id": origin_event_id,
        "nonce": verified_proof["nonce"],
        "issued_at": verified_proof["issued_at"],
        "expires_at": verified_proof["expires_at"],
        "proof_handle_ref": verified_proof["proof_handle_ref"],
        "proof_handle_digest": verified_proof["proof_handle_digest"],
        "proof_replay_slot": verified_proof["replay_slot"],
        "allowed_next_states": ["unavailable"],
        "proposal_digest": proposal_digest,
        "intent_payload_digest": stage1,
        "trusted_envelope_digest": trusted_envelope_digest(stage2_payload),
    }
