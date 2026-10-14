"""Phase 4 canonical intent contract tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
import unittest

from src.agent_cli.catalog import load_capability_catalog
from src.core.agent_trust.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    parse_canonical_json,
)
from src.core.agent_trust.contracts import (
    AgentTrustError,
    ApprovalReceiptV1,
    HumanPresenceProofClaimsV1,
    IntentProposalV1,
    OperationReceiptV1,
    TrustedIntentEnvelopeV1,
    VerifiedHostUserIntentClaimsV1,
)
from src.core.agent_trust.intent import (
    construct_core_direct_safe_intent,
    intent_payload_digest,
    prepare_intent_proposal,
    trusted_envelope_digest,
)


class CanonicalIntentTests(unittest.TestCase):
    def test_stage1_known_vector_covers_the_complete_contract_payload(self) -> None:
        payload = {
            "typed_request_complete_object": {"request_id": "req-001"},
            "typed_request_selector_and_all_dto_fields": {"request_id": "req-001"},
            "capability_id": "SYS-01",
            "command_id": "system.info",
            "normalized_argv_tokens": ["lulu", "system", "info"],
            "typed_target_refs": [],
            "opaque_artifact_refs": [],
            "account_ref_or_null": None,
            "tenant_ref_or_null": None,
            "record_and_resource_revisions": {},
            "idempotency_key_or_null": None,
            "request_schema_profile": "REQ_EMPTY_V1",
            "result_schema_profile": "RES_SYSTEM_INFO_V1",
            "effect_profile": "EFFECT_LOCAL_READ",
            "approval_profile": "APPROVAL_NONE",
            "lock_profile": "LOCK_NONE",
            "job_profile": "JOB_NONE",
            "oracle_profile": "ORACLE_SYSTEM_INFO",
        }
        expected_canonical = (
            b'{"account_ref_or_null":null,"approval_profile":"APPROVAL_NONE",'
            b'"capability_id":"SYS-01","command_id":"system.info",'
            b'"effect_profile":"EFFECT_LOCAL_READ","idempotency_key_or_null":null,'
            b'"job_profile":"JOB_NONE","lock_profile":"LOCK_NONE",'
            b'"normalized_argv_tokens":["lulu","system","info"],'
            b'"opaque_artifact_refs":[],"oracle_profile":"ORACLE_SYSTEM_INFO",'
            b'"record_and_resource_revisions":{},'
            b'"request_schema_profile":"REQ_EMPTY_V1",'
            b'"result_schema_profile":"RES_SYSTEM_INFO_V1",'
            b'"tenant_ref_or_null":null,'
            b'"typed_request_complete_object":{"request_id":"req-001"},'
            b'"typed_request_selector_and_all_dto_fields":{"request_id":"req-001"},'
            b'"typed_target_refs":[]}'
        )

        self.assertEqual(canonical_json_bytes(payload), expected_canonical)
        self.assertEqual(
            intent_payload_digest(payload),
            "54d2a169f0e84dca8a1a0ce54dc802db7e4883cf1bd5a19ee3002895725e41c9",
        )

    def test_stage2_known_vector_adds_verified_authority_without_self_reference(self) -> None:
        payload = {
            "envelope_version": "lulu.trusted-intent/v1",
            "intent_payload_digest": (
                "54d2a169f0e84dca8a1a0ce54dc802db7e4883cf1bd5a19ee3002895725e41c9"
            ),
            "constructor_mode": "core_direct_safe_v1",
            "issuer_type": "core_direct_safe_constructor",
            "issuer_id": "lulu-core",
            "issuer_verification_key_id": None,
            "audience": "com.lulu.transcribe.core/intent/v1",
            "authenticated_principal_id_or_null": None,
            "authenticated_session_id_or_null": None,
            "actor_class": "core_service",
            "input_source": "core_closed_catalog",
            "local_uid": 501,
            "installation_id": "install-001",
            "origin_event_id": "event-001",
            "nonce": "nonce-001",
            "issued_at": "2026-08-20T08:00:00Z",
            "expires_at": "2026-08-20T08:02:00Z",
            "proposal_digest_or_null": None,
            "proof_handle_digest_or_null": None,
            "attestation_handle_digest_or_null": None,
            "proof_replay_slot_or_null": None,
            "attestation_replay_slot_or_null": None,
        }

        self.assertNotIn("trusted_envelope_digest", payload)
        self.assertEqual(
            trusted_envelope_digest(payload),
            "b0ab7e82d29c7469a186dc8489790afa57710dc72599eeb9e1e92b4e43a5bff8",
        )

    def test_direct_safe_constructor_derives_sys01_from_exact_catalog_evidence(self) -> None:
        catalog = load_capability_catalog()["catalog"]

        envelope = construct_core_direct_safe_intent(
            catalog=catalog,
            capability_id="SYS-01",
            typed_request={"request_id": "req-001", "detail_level": "summary"},
            normalized_argv_tokens=["lulu", "version"],
            typed_target_refs=[],
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key=None,
            local_uid=501,
            installation_id="install-001",
            origin_event_id="event-001",
            nonce="nonce-001",
            issued_at="2026-08-20T08:00:00Z",
            expires_at="2026-08-20T08:02:00Z",
        )

        self.assertEqual(envelope["envelope_version"], "lulu.trusted-intent/v1")
        self.assertEqual(envelope["constructor_mode"], "core_direct_safe_v1")
        self.assertEqual(envelope["capability_id"], "SYS-01")
        self.assertEqual(envelope["command_id"], "cap.sys-01")
        self.assertEqual(envelope["allowed_next_states"], ["unavailable"])
        self.assertEqual(
            next(
                item["current_cli_state"]
                for item in catalog["capabilities"]
                if item["capability_id"] == "SYS-01"
            ),
            "unavailable",
        )
        self.assertNotIn("proof_handle_ref", envelope)
        self.assertNotIn("attestation_handle_ref", envelope)
        self.assertEqual(len(envelope["intent_payload_digest"]), 64)
        self.assertEqual(len(envelope["trusted_envelope_digest"]), 64)

    def test_direct_safe_constructor_rejects_a_catalog_that_promotes_current_state(self) -> None:
        catalog = deepcopy(load_capability_catalog()["catalog"])
        capability = next(
            item for item in catalog["capabilities"] if item["capability_id"] == "SYS-01"
        )
        capability["current_cli_state"] = "available"

        with self.assertRaises(AgentTrustError) as raised:
            construct_core_direct_safe_intent(
                catalog=catalog,
                capability_id="SYS-01",
                typed_request={"request_id": "req-001", "detail_level": "summary"},
                normalized_argv_tokens=["lulu", "version"],
                typed_target_refs=[],
                opaque_artifact_refs=[],
                account_ref=None,
                tenant_ref=None,
                record_and_resource_revisions={},
                idempotency_key=None,
                local_uid=501,
                installation_id="install-001",
                origin_event_id="event-001",
                nonce="nonce-001",
                issued_at="2026-08-20T08:00:00Z",
                expires_at="2026-08-20T08:02:00Z",
            )

        self.assertEqual(raised.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            raised.exception.reason_code,
            "DIRECT_SAFE_CURRENT_STATE_MISMATCH",
        )

    def test_prepare_returns_only_an_untrusted_non_approving_proposal(self) -> None:
        proposal = prepare_intent_proposal(
            catalog=load_capability_catalog()["catalog"],
            producer="Skill",
            plan_id="plan-001",
            capability_id="SYS-01",
            typed_request={"request_id": "req-001", "detail_level": "summary"},
            normalized_argv_tokens=["lulu", "version"],
            typed_target_refs=[],
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key=None,
        )

        self.assertEqual(proposal["proposal_version"], "lulu.intent-proposal/v1")
        self.assertEqual(proposal["trust_level"], "untrusted_data_plane")
        self.assertFalse(proposal["approved"])
        self.assertFalse(proposal["dispatchable"])
        self.assertEqual(proposal["current_cli_state"], "unavailable")
        self.assertEqual(len(proposal["intent_payload_digest"]), 64)
        self.assertEqual(len(proposal["proposal_digest"]), 64)
        self.assertTrue(
            frozenset(proposal).isdisjoint(
                {
                    "issuer_type",
                    "issuer_id",
                    "proof_handle_ref",
                    "attestation_handle_ref",
                    "trusted_envelope_digest",
                    "allowed_next_states",
                }
            )
        )

    def test_contract_module_freezes_all_six_non_qt_phase4_dtos(self) -> None:
        dto_types = (
            IntentProposalV1,
            TrustedIntentEnvelopeV1,
            HumanPresenceProofClaimsV1,
            VerifiedHostUserIntentClaimsV1,
            ApprovalReceiptV1,
            OperationReceiptV1,
        )
        for dto_type in dto_types:
            with self.subTest(dto=dto_type.__name__):
                self.assertEqual(dto_type.__module__, "src.core.agent_trust.contracts")
                self.assertNotIn("Qt", repr(dto_type))

        operation = OperationReceiptV1(
            operation_receipt_id="operation-receipt-001",
            job_id="job-001",
            attempt_id="attempt-001",
            lineage_id="lineage-001",
            installation_id="install-001",
            canonical_operation_id="cap.col-08",
            effect_profile="EFFECT_LOCAL_WRITE",
            job_profile="JOB_LOCAL",
            approval_profile="APPROVAL_HUMAN_RECEIPT",
            idempotency_key="idem-001",
            intent_payload_digest="a" * 64,
            created_at="2026-08-20T08:00:00Z",
        )
        with self.assertRaises(FrozenInstanceError):
            operation.job_id = "job-attacker"

    def test_raw_canonical_parser_rejects_duplicate_nan_and_noncanonical_json(self) -> None:
        hostile_inputs = (
            b'{"field":1,"field":2}',
            b'{"field":NaN}',
            b'{ "field":1}',
        )
        for raw in hostile_inputs:
            with self.subTest(raw=raw):
                with self.assertRaises(CanonicalizationError):
                    parse_canonical_json(raw)

        self.assertEqual(
            parse_canonical_json(b'{"field":1,"nested":[true,null]}'),
            {"field": 1, "nested": [True, None]},
        )

    def test_direct_safe_constructor_binds_file_placeholder_to_typed_source(self) -> None:
        with self.assertRaises(AgentTrustError) as raised:
            construct_core_direct_safe_intent(
                catalog=load_capability_catalog()["catalog"],
                capability_id="TR-01",
                typed_request={
                    "request_id": "req-001",
                    "source": "/tmp/authoritative-source.wav",
                },
                normalized_argv_tokens=[
                    "lulu",
                    "transcribe",
                    "prepare",
                    "/tmp/attacker-source.wav",
                ],
                typed_target_refs=[],
                opaque_artifact_refs=[],
                account_ref=None,
                tenant_ref=None,
                record_and_resource_revisions={},
                idempotency_key=None,
                local_uid=501,
                installation_id="install-001",
                origin_event_id="event-001",
                nonce="nonce-001",
                issued_at="2026-08-20T08:00:00Z",
                expires_at="2026-08-20T08:02:00Z",
            )

        self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
        self.assertEqual(
            raised.exception.reason_code,
            "TYPED_REQUEST_ARGV_BINDING_MISMATCH",
        )


if __name__ == "__main__":
    unittest.main()
