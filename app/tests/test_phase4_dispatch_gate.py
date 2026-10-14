"""Phase 4 dispatch gate contract tests."""

from __future__ import annotations

from copy import deepcopy
import unittest

from src.agent_cli.catalog import load_capability_catalog
from src.core.agent_trust.authority import (
    verify_host_user_intent,
    verify_platform_human_presence,
)
from src.core.agent_trust.contracts import AgentTrustError
from src.core.agent_trust.dispatch import evaluate_dispatch_gate
from src.core.agent_trust.intent import (
    construct_core_direct_safe_intent,
    construct_platform_human_presence_intent,
    construct_verified_host_intent,
    prepare_intent_proposal,
)
from src.core.agent_trust.replay import MemoryReplayStore


def _direct_safe_envelope() -> dict[str, object]:
    return construct_core_direct_safe_intent(
        catalog=load_capability_catalog()["catalog"],
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


class _DispatchSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, envelope: dict[str, object]) -> None:
        del envelope
        self.calls += 1


class _HostVerifier:
    def __init__(self, intent_payload_digest: str) -> None:
        self.intent_payload_digest = intent_payload_digest

    def verify(self, handle_ref: str) -> dict[str, object]:
        del handle_ref
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
            "intent_payload_digest": self.intent_payload_digest,
            "replay_slot": "host-slot-001",
        }


class _PlatformVerifier:
    def __init__(self, intent_payload_digest: str) -> None:
        self.intent_payload_digest = intent_payload_digest

    def verify(self, handle_ref: str) -> dict[str, object]:
        del handle_ref
        return {
            "proof_version": "HUMAN_PRESENCE_PROOF_V1",
            "issuer_id": "platform-issuer",
            "issuer_verification_key_id": "platform-key-v1",
            "audience": "com.lulu.transcribe.core/approval/v1",
            "local_uid": 501,
            "installation_id": "install-001",
            "nonce": "nonce-002",
            "approval_id": "approval-002",
            "intent_payload_digest": self.intent_payload_digest,
            "issued_at": 1_776_864_000.0,
            "expires_at": 1_776_864_120.0,
            "monotonic_issued_at": 42_000.0,
            "monotonic_expires_at": 42_120.0,
            "replay_slot": "proof-slot-002",
        }


def _verified_host_envelope() -> dict[str, object]:
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
    verified = verify_host_user_intent(
        handle_ref="opaque-host-handle",
        verifier=_HostVerifier(proposal["intent_payload_digest"]),
        replay_store=MemoryReplayStore(),
        pinned_issuers={"host-issuer": frozenset({"host-key-v1"})},
        expected_intent_payload_digest=proposal["intent_payload_digest"],
        expected_local_uid=501,
        expected_installation_id="install-001",
        expected_nonce="nonce-001",
        now_wall=1_776_864_060.0,
        now_monotonic=42_060.0,
    )
    return construct_verified_host_intent(
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


def _platform_envelope() -> dict[str, object]:
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
    verified = verify_platform_human_presence(
        handle_ref="opaque-platform-handle-002",
        verifier=_PlatformVerifier(proposal["intent_payload_digest"]),
        pinned_issuers={"platform-issuer": frozenset({"platform-key-v1"})},
        expected_approval_id="approval-002",
        expected_intent_payload_digest=proposal["intent_payload_digest"],
        expected_local_uid=501,
        expected_installation_id="install-001",
        expected_nonce="nonce-002",
        now_wall=1_776_864_060.0,
        now_monotonic=42_060.0,
    )
    return construct_platform_human_presence_intent(
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


class DispatchGateTests(unittest.TestCase):
    def test_command_tampering_fails_digest_recomputation_before_dispatch(self) -> None:
        envelope = deepcopy(_direct_safe_envelope())
        envelope["command_id"] = "cap.sys-02"
        dispatcher = _DispatchSpy()

        with self.assertRaises(AgentTrustError) as raised:
            evaluate_dispatch_gate(
                envelope=envelope,
                catalog=load_capability_catalog()["catalog"],
                business_dispatcher=dispatcher,
            )

        self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
        self.assertEqual(
            raised.exception.reason_code,
            "INTENT_PAYLOAD_DIGEST_MISMATCH",
        )
        self.assertEqual(dispatcher.calls, 0)

    def test_verified_direct_safe_intent_remains_unavailable_and_rejects_catalog_promotion(self) -> None:
        catalog = deepcopy(load_capability_catalog()["catalog"])
        dispatcher = _DispatchSpy()

        result = evaluate_dispatch_gate(
            envelope=_direct_safe_envelope(),
            catalog=catalog,
            business_dispatcher=dispatcher,
        )

        self.assertEqual(result["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            result["reason_code"],
            "VERIFIED_BUT_CAPABILITY_UNAVAILABLE",
        )
        self.assertEqual(result["exit_code"], 30)
        self.assertEqual(result["business_dispatch_count"], 0)
        self.assertEqual(dispatcher.calls, 0)

        capability = next(
            item for item in catalog["capabilities"] if item["capability_id"] == "SYS-01"
        )
        capability["current_cli_state"] = "available"
        with self.assertRaises(AgentTrustError) as raised:
            evaluate_dispatch_gate(
                envelope=_direct_safe_envelope(),
                catalog=catalog,
                business_dispatcher=dispatcher,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "DIRECT_SAFE_CURRENT_STATE_MISMATCH",
        )
        self.assertEqual(dispatcher.calls, 0)

    def test_verified_host_envelope_is_recomputed_then_stays_unavailable(self) -> None:
        dispatcher = _DispatchSpy()
        envelope = _verified_host_envelope()

        result = evaluate_dispatch_gate(
            envelope=envelope,
            catalog=load_capability_catalog()["catalog"],
            business_dispatcher=dispatcher,
        )
        self.assertEqual(
            result["reason_code"],
            "VERIFIED_BUT_CAPABILITY_UNAVAILABLE",
        )
        self.assertEqual(dispatcher.calls, 0)

        tampered = deepcopy(envelope)
        tampered["attestation_handle_digest"] = "f" * 64
        with self.assertRaises(AgentTrustError) as raised:
            evaluate_dispatch_gate(
                envelope=tampered,
                catalog=load_capability_catalog()["catalog"],
                business_dispatcher=dispatcher,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "TRUSTED_ENVELOPE_DIGEST_MISMATCH",
        )
        self.assertEqual(dispatcher.calls, 0)

    def test_proposal_digest_replacement_invalidates_stage2_before_dispatch(self) -> None:
        dispatcher = _DispatchSpy()
        tampered = deepcopy(_verified_host_envelope())
        tampered["proposal_digest"] = "f" * 64

        with self.assertRaises(AgentTrustError) as raised:
            evaluate_dispatch_gate(
                envelope=tampered,
                catalog=load_capability_catalog()["catalog"],
                business_dispatcher=dispatcher,
            )

        self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
        self.assertEqual(
            raised.exception.reason_code,
            "TRUSTED_ENVELOPE_DIGEST_MISMATCH",
        )
        self.assertEqual(dispatcher.calls, 0)

    def test_platform_envelope_is_recomputed_then_stays_unavailable(self) -> None:
        dispatcher = _DispatchSpy()
        envelope = _platform_envelope()

        result = evaluate_dispatch_gate(
            envelope=envelope,
            catalog=load_capability_catalog()["catalog"],
            business_dispatcher=dispatcher,
        )
        self.assertEqual(
            result["reason_code"],
            "VERIFIED_BUT_CAPABILITY_UNAVAILABLE",
        )
        self.assertEqual(dispatcher.calls, 0)

        tampered = deepcopy(envelope)
        tampered["proof_handle_digest"] = "f" * 64
        with self.assertRaises(AgentTrustError) as raised:
            evaluate_dispatch_gate(
                envelope=tampered,
                catalog=load_capability_catalog()["catalog"],
                business_dispatcher=dispatcher,
            )
        self.assertEqual(
            raised.exception.reason_code,
            "TRUSTED_ENVELOPE_DIGEST_MISMATCH",
        )
        self.assertEqual(dispatcher.calls, 0)


if __name__ == "__main__":
    unittest.main()
