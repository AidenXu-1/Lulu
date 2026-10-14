"""Phase 4 authority verification contract tests."""

from __future__ import annotations

import unittest

from src.agent_cli.catalog import load_capability_catalog
from src.core.agent_trust.authority import (
    UnavailableHostIntentVerifier,
    UnavailablePlatformHumanPresenceVerifier,
    verify_host_user_intent,
    verify_platform_human_presence,
)
from src.core.agent_trust.contracts import AgentTrustError
from src.core.agent_trust.intent import (
    construct_platform_human_presence_intent,
    construct_verified_host_intent,
    prepare_intent_proposal,
)
from src.core.agent_trust.replay import MemoryReplayStore


class _RecordingReplayStore:
    def __init__(self) -> None:
        self.consume_calls = 0

    def consume(self, slot: str) -> None:
        self.consume_calls += 1


class _StaticVerifier:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims

    def verify(self, handle_ref: str) -> dict[str, object]:
        self.last_handle_ref = handle_ref
        return dict(self.claims)


def _valid_host_claims() -> dict[str, object]:
    return {
        "issuer_id": "host-issuer",
        "issuer_verification_key_id": "host-key-v1",
        "audience": "com.lulu.transcribe.core/intent/v1",
        "authenticated_principal_id": "human-001",
        "authenticated_session_id": "session-001",
        "actor_class": "authenticated_human_user",
        "input_source": "host_authenticated_user_instruction",
        "origin_event_id": "event-001",
        "local_uid": 501,
        "installation_id": "install-001",
        "nonce": "nonce-001",
        "issued_at": 1_776_864_000.0,
        "expires_at": 1_776_864_120.0,
        "monotonic_issued_at": 42_000.0,
        "monotonic_expires_at": 42_120.0,
        "intent_payload_digest": "a" * 64,
        "replay_slot": "host-slot-001",
    }


class AuthorityVerificationTests(unittest.TestCase):
    def test_default_host_verifier_is_unavailable_without_replay_mutation(self) -> None:
        replay_store = _RecordingReplayStore()

        with self.assertRaises(AgentTrustError) as raised:
            verify_host_user_intent(
                handle_ref="opaque-host-handle",
                verifier=UnavailableHostIntentVerifier(),
                replay_store=replay_store,
                pinned_issuers={"host-issuer": frozenset({"host-key-v1"})},
                expected_intent_payload_digest="a" * 64,
                expected_local_uid=501,
                expected_installation_id="install-001",
                expected_nonce="nonce-001",
                now_wall=1_776_864_060.0,
                now_monotonic=42_060.0,
            )

        self.assertEqual(raised.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            raised.exception.reason_code,
            "HOST_INTENT_VERIFIER_UNAVAILABLE",
        )
        self.assertEqual(replay_store.consume_calls, 0)

    def test_host_verifier_rejects_every_automation_input_source_before_replay(self) -> None:
        forbidden_sources = (
            "accessibility_api",
            "synthetic_input",
            "applescript",
            "remote_control",
            "agent_originated_event",
            "skill_originated_event",
            "replayed_event",
            "unknown",
        )
        for source in forbidden_sources:
            with self.subTest(source=source):
                claims = _valid_host_claims()
                claims["input_source"] = source
                replay_store = _RecordingReplayStore()

                with self.assertRaises(AgentTrustError) as raised:
                    verify_host_user_intent(
                        handle_ref="opaque-host-handle",
                        verifier=_StaticVerifier(claims),
                        replay_store=replay_store,
                        pinned_issuers={"host-issuer": frozenset({"host-key-v1"})},
                        expected_intent_payload_digest="a" * 64,
                        expected_local_uid=501,
                        expected_installation_id="install-001",
                        expected_nonce="nonce-001",
                        now_wall=1_776_864_060.0,
                        now_monotonic=42_060.0,
                    )

                self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
                self.assertEqual(
                    raised.exception.reason_code,
                    "HOST_INPUT_SOURCE_FORBIDDEN",
                )
                self.assertEqual(replay_store.consume_calls, 0)

    def test_verified_host_handle_is_consumed_exactly_once(self) -> None:
        verifier = _StaticVerifier(_valid_host_claims())
        replay_store = MemoryReplayStore()
        arguments = {
            "handle_ref": "opaque-host-handle",
            "verifier": verifier,
            "replay_store": replay_store,
            "pinned_issuers": {"host-issuer": frozenset({"host-key-v1"})},
            "expected_intent_payload_digest": "a" * 64,
            "expected_local_uid": 501,
            "expected_installation_id": "install-001",
            "expected_nonce": "nonce-001",
            "now_wall": 1_776_864_060.0,
            "now_monotonic": 42_060.0,
        }

        verified = verify_host_user_intent(**arguments)
        self.assertEqual(verified["actor_class"], "authenticated_human_user")
        self.assertEqual(verified["replay_slot"], "host-slot-001")
        self.assertEqual(replay_store.mutation_count, 1)

        with self.assertRaises(AgentTrustError) as raised:
            verify_host_user_intent(**arguments)

        self.assertEqual(raised.exception.code, "APPROVAL_CONSUMED")
        self.assertEqual(replay_store.mutation_count, 1)

    def test_default_platform_verifier_is_unavailable_without_real_os_proof(self) -> None:
        with self.assertRaises(AgentTrustError) as raised:
            verify_platform_human_presence(
                handle_ref="opaque-platform-handle",
                verifier=UnavailablePlatformHumanPresenceVerifier(),
                pinned_issuers={"platform-issuer": frozenset({"platform-key-v1"})},
                expected_approval_id="approval-001",
                expected_intent_payload_digest="a" * 64,
                expected_local_uid=501,
                expected_installation_id="install-001",
                expected_nonce="nonce-001",
                now_wall=1_776_864_060.0,
                now_monotonic=42_060.0,
            )

        self.assertEqual(raised.exception.code, "CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            raised.exception.reason_code,
            "PLATFORM_PROOF_VERIFIER_UNAVAILABLE",
        )

    def test_verified_host_claims_construct_stage2_without_promoting_capability(self) -> None:
        catalog = load_capability_catalog()["catalog"]
        request = {
            "request_id": "req-001",
            "source": "/tmp/fixture.wav",
            "engine": "sensevoice",
        }
        argv = ["lulu", "transcribe", "run", "/tmp/fixture.wav"]
        targets = [{"kind": "source_file", "id": "fixture.wav"}]
        proposal = prepare_intent_proposal(
            catalog=catalog,
            producer="Agent",
            plan_id="plan-001",
            capability_id="TR-02",
            typed_request=request,
            normalized_argv_tokens=argv,
            typed_target_refs=targets,
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key=None,
        )
        replay_store = MemoryReplayStore()
        claims = _valid_host_claims()
        claims["intent_payload_digest"] = proposal["intent_payload_digest"]
        verified = verify_host_user_intent(
            handle_ref="opaque-host-handle",
            verifier=_StaticVerifier(claims),
            replay_store=replay_store,
            pinned_issuers={"host-issuer": frozenset({"host-key-v1"})},
            expected_intent_payload_digest=proposal["intent_payload_digest"],
            expected_local_uid=501,
            expected_installation_id="install-001",
            expected_nonce="nonce-001",
            now_wall=1_776_864_060.0,
            now_monotonic=42_060.0,
        )

        envelope = construct_verified_host_intent(
            catalog=catalog,
            capability_id="TR-02",
            typed_request=request,
            normalized_argv_tokens=argv,
            typed_target_refs=targets,
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key=None,
            proposal_digest=proposal["proposal_digest"],
            verified_authority=verified,
        )

        self.assertEqual(envelope["constructor_mode"], "verified_host_user_intent")
        self.assertEqual(envelope["authenticated_principal_id"], "human-001")
        self.assertEqual(envelope["authenticated_session_id"], "session-001")
        self.assertEqual(envelope["actor_class"], "authenticated_human_user")
        self.assertEqual(
            envelope["input_source"],
            "host_authenticated_user_instruction",
        )
        self.assertIn("attestation_handle_ref", envelope)
        self.assertIn("attestation_handle_digest", envelope)
        self.assertNotIn("proof_handle_ref", envelope)
        self.assertEqual(envelope["allowed_next_states"], ["unavailable"])
        self.assertEqual(len(envelope["trusted_envelope_digest"]), 64)
        self.assertEqual(replay_store.mutation_count, 1)

    def test_platform_proof_constructs_stage2_without_real_os_or_business_effect(self) -> None:
        catalog = load_capability_catalog()["catalog"]
        request = {"request_id": "req-002", "operation": "close"}
        argv = ["lulu", "collect", "browser", "close"]
        targets = [{"kind": "browser_session", "id": "dedicated"}]
        proposal = prepare_intent_proposal(
            catalog=catalog,
            producer="Agent",
            plan_id="plan-002",
            capability_id="COL-08",
            typed_request=request,
            normalized_argv_tokens=argv,
            typed_target_refs=targets,
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key="idem-002",
        )
        claims = {
            "proof_version": "HUMAN_PRESENCE_PROOF_V1",
            "issuer_id": "platform-issuer",
            "issuer_verification_key_id": "platform-key-v1",
            "audience": "com.lulu.transcribe.core/approval/v1",
            "local_uid": 501,
            "installation_id": "install-001",
            "nonce": "nonce-002",
            "approval_id": "approval-002",
            "intent_payload_digest": proposal["intent_payload_digest"],
            "issued_at": 1_776_864_000.0,
            "expires_at": 1_776_864_120.0,
            "monotonic_issued_at": 42_000.0,
            "monotonic_expires_at": 42_120.0,
            "replay_slot": "proof-slot-002",
        }
        verified = verify_platform_human_presence(
            handle_ref="opaque-platform-handle-002",
            verifier=_StaticVerifier(claims),
            pinned_issuers={"platform-issuer": frozenset({"platform-key-v1"})},
            expected_approval_id="approval-002",
            expected_intent_payload_digest=proposal["intent_payload_digest"],
            expected_local_uid=501,
            expected_installation_id="install-001",
            expected_nonce="nonce-002",
            now_wall=1_776_864_060.0,
            now_monotonic=42_060.0,
        )

        envelope = construct_platform_human_presence_intent(
            catalog=catalog,
            capability_id="COL-08",
            typed_request=request,
            normalized_argv_tokens=argv,
            typed_target_refs=targets,
            opaque_artifact_refs=[],
            account_ref=None,
            tenant_ref=None,
            record_and_resource_revisions={},
            idempotency_key="idem-002",
            proposal_digest=proposal["proposal_digest"],
            verified_proof=verified,
        )

        self.assertEqual(
            envelope["constructor_mode"],
            "platform_human_presence_fallback",
        )
        self.assertIn("proof_handle_ref", envelope)
        self.assertIn("proof_handle_digest", envelope)
        self.assertNotIn("attestation_handle_ref", envelope)
        self.assertEqual(envelope["allowed_next_states"], ["unavailable"])
        self.assertEqual(len(envelope["trusted_envelope_digest"]), 64)


if __name__ == "__main__":
    unittest.main()
