"""Closed adapter from Skill candidate data to the Core proposal seam."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re


_REQUEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "plan_id",
        "capability_id",
        "typed_request",
        "normalized_argv_tokens",
        "typed_target_refs",
        "opaque_artifact_refs",
        "account_ref_or_null",
        "tenant_ref_or_null",
        "record_and_resource_revisions",
        "idempotency_key_or_null",
    }
)
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "license_code",
        "license_key",
        "secret",
        "token",
        "access_token",
        "auth_token",
        "bearer_token",
        "credential",
        "password",
    }
)
_MAX_JSON_BYTES = 65_536
_MAX_STRING_BYTES = 4_096
_MAX_COLLECTION_ITEMS = 128
_MAX_DEPTH = 8
_ARTIFACT_REQUIRED_FIELDS = frozenset({"artifact_id", "kind", "sha256", "revision"})
_ARTIFACT_OPTIONAL_FIELDS = frozenset({"label"})
_ARTIFACT_KINDS = frozenset(
    {
        "untrusted_text",
        "local_artifact",
        "transcript_ref",
        "web_content_ref",
        "model_output_ref",
    }
)
_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SkillProposalError(ValueError):
    """Untrusted Skill data does not match the closed proposal request."""

    def __init__(self, code: str, field: str, reason_code: str) -> None:
        super().__init__(f"{code}:{field}:{reason_code}")
        self.code = code
        self.field = field
        self.reason_code = reason_code


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SkillProposalError("INVALID_ARGUMENT", "request_json", "DUPLICATE_FIELD")
        result[key] = value
    return result


def parse_skill_request_json(raw: str) -> dict[str, object]:
    """Parse one bounded object while rejecting duplicate keys and non-finite numbers."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise SkillProposalError("INVALID_ARGUMENT", "request_json", "VALUE_TOO_LARGE")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SkillProposalError("INVALID_ARGUMENT", "request_json", "NON_FINITE_NUMBER")
            ),
        )
    except SkillProposalError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SkillProposalError("INVALID_ARGUMENT", "request_json", "JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise SkillProposalError("INVALID_ARGUMENT", "request_json", "OBJECT_REQUIRED")
    return value


def _bounded(value: object, *, field: str, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise SkillProposalError("INVALID_ARGUMENT", field, "NESTING_TOO_DEEP")
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, float):
        raise SkillProposalError("INVALID_ARGUMENT", field, "NUMBER_TYPE_FORBIDDEN")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_STRING_BYTES:
            raise SkillProposalError("INVALID_ARGUMENT", field, "VALUE_TOO_LARGE")
        return
    if isinstance(value, list):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise SkillProposalError("INVALID_ARGUMENT", field, "TOO_MANY_ITEMS")
        for index, item in enumerate(value):
            _bounded(item, field=f"{field}.{index}", depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise SkillProposalError("INVALID_ARGUMENT", field, "TOO_MANY_FIELDS")
        for key, item in value.items():
            if not isinstance(key, str):
                raise SkillProposalError("INVALID_ARGUMENT", field, "FIELD_NAME_INVALID")
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                raise SkillProposalError("INVALID_ARGUMENT", field, "SECRET_INPUT_FORBIDDEN")
            _bounded(item, field=f"{field}.{key}", depth=depth + 1)
        return
    raise SkillProposalError("INVALID_ARGUMENT", field, "VALUE_TYPE_INVALID")


def _validate_artifact_refs(value: list[object]) -> None:
    if len(value) > _MAX_COLLECTION_ITEMS:
        raise SkillProposalError("INVALID_ARGUMENT", "opaque_artifact_refs", "TOO_MANY_ITEMS")
    for index, item in enumerate(value):
        field = f"opaque_artifact_refs.{index}"
        if not isinstance(item, Mapping):
            raise SkillProposalError("INVALID_ARGUMENT", field, "OBJECT_REQUIRED")
        actual = set(item)
        unknown = actual - _ARTIFACT_REQUIRED_FIELDS - _ARTIFACT_OPTIONAL_FIELDS
        missing = _ARTIFACT_REQUIRED_FIELDS - actual
        if unknown:
            raise SkillProposalError(
                "UNKNOWN_FIELD",
                f"{field}.{sorted(unknown)[0]}",
                "ARTIFACT_REF_FIELD_NOT_ALLOWED",
            )
        if missing:
            raise SkillProposalError(
                "INVALID_ARGUMENT",
                f"{field}.{sorted(missing)[0]}",
                "ARTIFACT_REF_FIELD_REQUIRED",
            )
        if not isinstance(item["artifact_id"], str) or not _SAFE_REF.fullmatch(item["artifact_id"]):
            raise SkillProposalError("INVALID_ARGUMENT", f"{field}.artifact_id", "ARTIFACT_REF_INVALID")
        if item["kind"] not in _ARTIFACT_KINDS:
            raise SkillProposalError("INVALID_ARGUMENT", f"{field}.kind", "ARTIFACT_KIND_UNKNOWN")
        if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
            raise SkillProposalError("INVALID_ARGUMENT", f"{field}.sha256", "ARTIFACT_REF_INVALID")
        if not isinstance(item["revision"], str) or not _SAFE_REF.fullmatch(item["revision"]):
            raise SkillProposalError("INVALID_ARGUMENT", f"{field}.revision", "ARTIFACT_REF_INVALID")
        label = item.get("label")
        if label is not None and (
            not isinstance(label, str) or len(label.encode("utf-8")) > 512
        ):
            raise SkillProposalError("INVALID_ARGUMENT", f"{field}.label", "ARTIFACT_LABEL_INVALID")


def build_skill_proposal(request: Mapping[str, object]) -> dict[str, object]:
    """Build only ``lulu.intent-proposal/v1`` from a complete candidate request."""

    if not isinstance(request, Mapping):
        raise SkillProposalError("INVALID_ARGUMENT", "request", "OBJECT_REQUIRED")
    actual = set(request)
    unknown = actual - _REQUEST_FIELDS
    missing = _REQUEST_FIELDS - actual
    if unknown:
        raise SkillProposalError("UNKNOWN_FIELD", sorted(unknown)[0], "FIELD_NOT_ALLOWED")
    if missing:
        raise SkillProposalError("INVALID_ARGUMENT", sorted(missing)[0], "FIELD_REQUIRED")
    if request["schema_id"] != "lulu.skill-proposal-request/v1" or request["schema_revision"] != 1:
        raise SkillProposalError("INVALID_ARGUMENT", "schema", "SCHEMA_INCOMPATIBLE")
    for field in ("plan_id", "capability_id"):
        if type(request[field]) is not str or not request[field]:
            raise SkillProposalError("INVALID_ARGUMENT", field, "NONEMPTY_STRING_REQUIRED")
    if not isinstance(request["typed_request"], Mapping):
        raise SkillProposalError("INVALID_ARGUMENT", "typed_request", "OBJECT_REQUIRED")
    if not isinstance(request["normalized_argv_tokens"], list):
        raise SkillProposalError("INVALID_ARGUMENT", "normalized_argv_tokens", "ARRAY_REQUIRED")
    if not isinstance(request["typed_target_refs"], list):
        raise SkillProposalError("INVALID_ARGUMENT", "typed_target_refs", "ARRAY_REQUIRED")
    if not isinstance(request["opaque_artifact_refs"], list):
        raise SkillProposalError("INVALID_ARGUMENT", "opaque_artifact_refs", "ARRAY_REQUIRED")
    if not isinstance(request["record_and_resource_revisions"], Mapping):
        raise SkillProposalError("INVALID_ARGUMENT", "record_and_resource_revisions", "OBJECT_REQUIRED")
    _validate_artifact_refs(request["opaque_artifact_refs"])
    _bounded(request, field="request")

    # Imported only for the explicit Phase 9 proposal command. Source-mode Phase 3
    # controls remain free of Core business/runtime imports.
    from src.agent_cli.catalog import load_capability_catalog
    from src.core.agent_trust.intent import prepare_intent_proposal

    try:
        return prepare_intent_proposal(
            catalog=load_capability_catalog()["catalog"],
            producer="Skill",
            plan_id=request["plan_id"],
            capability_id=request["capability_id"],
            typed_request=request["typed_request"],
            normalized_argv_tokens=request["normalized_argv_tokens"],
            typed_target_refs=request["typed_target_refs"],
            opaque_artifact_refs=request["opaque_artifact_refs"],
            account_ref=request["account_ref_or_null"],
            tenant_ref=request["tenant_ref_or_null"],
            record_and_resource_revisions=request["record_and_resource_revisions"],
            idempotency_key=request["idempotency_key_or_null"],
        )
    except SkillProposalError:
        raise
    except Exception as exc:
        reason_code = str(getattr(exc, "reason_code", "PROPOSAL_REJECTED"))
        if not reason_code.isascii() or not reason_code.replace("_", "").isalnum():
            reason_code = "PROPOSAL_REJECTED"
        raise SkillProposalError("INVALID_ARGUMENT", "request", reason_code) from exc
