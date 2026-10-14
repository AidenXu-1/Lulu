"""Phase 4 approval receipt and ledger contract tests."""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
import os
import stat
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from src.agent_cli.catalog import load_capability_catalog
from src.core.agent_trust.approval import (
    create_approval_receipt,
    create_operation_receipt,
)
from src.core.agent_trust.authority import (
    UnavailablePlatformHumanPresenceVerifier,
    verify_platform_human_presence,
)
from src.core.agent_trust.contracts import AgentTrustError
from src.core.agent_trust.canonical import canonical_json_bytes
from src.core.agent_trust.intent import (
    construct_platform_human_presence_intent,
    prepare_intent_proposal,
)
from src.core.agent_trust.ledger import ApprovalLedger
import src.core.agent_trust.ledger as ledger_module


class FakeIntegrityAnchorStore:
    def __init__(self) -> None:
        self.state: dict[str, object] | None = None
        self.mutation_count = 0
        self.fail_next_compare_and_set = False

    def hmac_key(self) -> bytes:
        return b"phase4-test-hmac-key-material-32"

    def load(self) -> dict[str, object] | None:
        return deepcopy(self.state)

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None:
        if self.fail_next_compare_and_set:
            self.fail_next_compare_and_set = False
            raise RuntimeError("simulated anchor crash window")
        if self.state != expected:
            raise RuntimeError("anchor changed")
        self.state = deepcopy(replacement)
        self.mutation_count += 1


class _StaticPlatformVerifier:
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
            "nonce": "nonce-001",
            "approval_id": "approval-001",
            "intent_payload_digest": self.intent_payload_digest,
            "issued_at": 1_776_864_000.0,
            "expires_at": 1_776_864_120.0,
            "monotonic_issued_at": 42_000.0,
            "monotonic_expires_at": 42_120.0,
            "replay_slot": "proof-slot-001",
        }


class _ForgedIssuerPlatformVerifier(_StaticPlatformVerifier):
    def verify(self, handle_ref: str) -> dict[str, object]:
        claims = super().verify(handle_ref)
        claims["issuer_id"] = "attacker-issuer"
        return claims


class _FailingLock:
    def __enter__(self):
        raise RuntimeError("simulated lock acquisition failure")

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        return False


def _platform_chain() -> tuple[dict[str, object], dict[str, object], list[str]]:
    catalog = load_capability_catalog()["catalog"]
    request = {"request_id": "req-001", "operation": "close"}
    argv = ["lulu", "collect", "browser", "close"]
    targets = [{"kind": "browser_session", "id": "dedicated"}]
    proposal = prepare_intent_proposal(
        catalog=catalog,
        producer="Agent",
        plan_id="plan-001",
        capability_id="COL-08",
        typed_request=request,
        normalized_argv_tokens=argv,
        typed_target_refs=targets,
        opaque_artifact_refs=[],
        account_ref="tenant-001",
        tenant_ref="tenant-001",
        record_and_resource_revisions={"dedicated": 7},
        idempotency_key="idem-001",
    )
    proof = verify_platform_human_presence(
        handle_ref="opaque-platform-handle",
        verifier=_StaticPlatformVerifier(proposal["intent_payload_digest"]),
        pinned_issuers={"platform-issuer": frozenset({"platform-key-v1"})},
        expected_approval_id="approval-001",
        expected_intent_payload_digest=proposal["intent_payload_digest"],
        expected_local_uid=501,
        expected_installation_id="install-001",
        expected_nonce="nonce-001",
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
        account_ref="tenant-001",
        tenant_ref="tenant-001",
        record_and_resource_revisions={"dedicated": 7},
        idempotency_key="idem-001",
        proposal_digest=proposal["proposal_digest"],
        verified_proof=proof,
    )
    return proof, envelope, argv


def _verified_platform_proof() -> dict[str, object]:
    return _platform_chain()[0]


def _approval_receipt():
    proof, envelope, argv = _platform_chain()
    return create_approval_receipt(
        verified_proof=proof,
        plan_id="plan-001",
        capability_id="COL-08",
        job_id="job-001",
        attempt_id="attempt-001",
        lineage_id="lineage-001",
        canonical_operation_id="cap.col-08",
        trusted_envelope=envelope,
        normalized_argv_tokens=argv,
        signed_manifest_sha256="c" * 64,
        account_or_tenant_ref="tenant-001",
        target_ref="browser_session:dedicated",
        target_identity={"kind": "browser_session", "id": "dedicated"},
        input_fingerprints=["sha256:" + "d" * 64],
        record_revisions={"dedicated": 7},
        issuer_channel="platform_verified_human_presence",
    )


def _append_verified_approval(
    ledger: ApprovalLedger,
    *,
    receipt=None,
):
    bound_receipt = receipt or _approval_receipt()
    return ledger.verify_and_append_approved(
        handle_ref="opaque-platform-handle",
        verifier=_StaticPlatformVerifier(bound_receipt.intent_payload_digest),
        pinned_issuers={"platform-issuer": frozenset({"platform-key-v1"})},
        expected_approval_id="approval-001",
        expected_intent_payload_digest=bound_receipt.intent_payload_digest,
        expected_local_uid=501,
        expected_installation_id="install-001",
        expected_nonce="nonce-001",
        now_wall=1_776_864_060.0,
        now_monotonic=42_060.0,
        receipt_builder=lambda _proof: bound_receipt,
    )


class ApprovalLedgerTests(unittest.TestCase):
    def test_all_payload_entrypoints_reject_forged_approved_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as probe_directory:
            probe = ApprovalLedger(
                Path(probe_directory).resolve() / "approval-ledger",
                FakeIntegrityAnchorStore(),
            )
            payload_entrypoints = []
            for name in dir(probe):
                candidate = getattr(probe, name)
                if not callable(candidate):
                    continue
                try:
                    parameters = inspect.signature(candidate).parameters
                except (TypeError, ValueError):
                    continue
                if "payload" in parameters:
                    payload_entrypoints.append(name)

        self.assertIn("_append_payload", payload_entrypoints)
        forged_approved = {
            "event": "approved",
            "approval_id": "approval-forged",
            "proof_replay_slot": "proof-slot-forged",
            "proof_handle_digest": "f" * 64,
            "receipt": {
                "approval_id": "approval-forged",
                "state": "approved",
            },
        }
        for entrypoint in payload_entrypoints:
            with self.subTest(entrypoint=entrypoint):
                with TemporaryDirectory() as temporary_directory:
                    anchor = FakeIntegrityAnchorStore()
                    ledger = ApprovalLedger(
                        Path(temporary_directory).resolve() / "approval-ledger",
                        anchor,
                    )
                    stable_head = ledger.head
                    stable_journal = ledger.journal_path.read_bytes()
                    stable_anchor_mutations = anchor.mutation_count
                    rejection = None
                    try:
                        getattr(ledger, entrypoint)(payload=forged_approved)
                    except AgentTrustError as exc:
                        rejection = exc

                    self.assertIsNone(
                        ledger.approval_state("approval-forged")
                    )
                    self.assertEqual(ledger.head, stable_head)
                    self.assertEqual(
                        ledger.journal_path.read_bytes(),
                        stable_journal,
                    )
                    self.assertEqual(
                        anchor.mutation_count,
                        stable_anchor_mutations,
                    )
                    self.assertIsNotNone(rejection)

    def test_instance_exposes_no_caller_forgeable_approved_append_path(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            anchor = FakeIntegrityAnchorStore()
            ledger = ApprovalLedger(
                Path(temporary_directory).resolve() / "approval-ledger",
                anchor,
            )
            stable_head = ledger.head
            stable_size = ledger.journal_path.stat().st_size
            stable_anchor_mutations = anchor.mutation_count

            exposed_writers: list[str] = []
            for name in dir(ledger):
                candidate = getattr(ledger, name)
                if not callable(candidate) or name == "verify_and_append_approved":
                    continue
                try:
                    parameters = inspect.signature(candidate).parameters
                except (TypeError, ValueError):
                    continue
                if {"receipt", "verified_proof"}.issubset(parameters):
                    exposed_writers.append(name)
                    try:
                        candidate(
                            receipt=_approval_receipt(),
                            verified_proof=_verified_platform_proof(),
                        )
                    except Exception:  # noqa: BLE001 - hostile caller probe
                        pass

            self.assertEqual(exposed_writers, [])
            self.assertIsNone(ledger.approval_state("approval-001"))
            self.assertEqual(ledger.head, stable_head)
            self.assertEqual(ledger.journal_path.stat().st_size, stable_size)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_unverified_platform_paths_leave_approval_ledger_unchanged(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            anchor = FakeIntegrityAnchorStore()
            ledger = ApprovalLedger(
                Path(temporary_directory).resolve() / "approval-ledger",
                anchor,
            )
            receipt = _approval_receipt()
            arguments = {
                "handle_ref": "opaque-platform-handle",
                "pinned_issuers": {
                    "platform-issuer": frozenset({"platform-key-v1"})
                },
                "expected_approval_id": "approval-001",
                "expected_intent_payload_digest": receipt.intent_payload_digest,
                "expected_local_uid": 501,
                "expected_installation_id": "install-001",
                "expected_nonce": "nonce-001",
                "now_wall": 1_776_864_060.0,
                "now_monotonic": 42_060.0,
                "receipt_builder": lambda _proof: receipt,
            }

            for verifier in (
                UnavailablePlatformHumanPresenceVerifier(),
                _ForgedIssuerPlatformVerifier(receipt.intent_payload_digest),
            ):
                with self.subTest(verifier=type(verifier).__name__):
                    stable_head = ledger.head
                    stable_size = ledger.journal_path.stat().st_size
                    stable_anchor_mutations = anchor.mutation_count
                    with self.assertRaises(AgentTrustError):
                        ledger.verify_and_append_approved(
                            verifier=verifier,
                            **arguments,
                        )
                    self.assertIsNone(ledger.approval_state("approval-001"))
                    self.assertEqual(ledger.head, stable_head)
                    self.assertEqual(ledger.journal_path.stat().st_size, stable_size)
                    self.assertEqual(
                        anchor.mutation_count,
                        stable_anchor_mutations,
                    )

            stable_head = ledger.head
            stable_size = ledger.journal_path.stat().st_size
            stable_anchor_mutations = anchor.mutation_count
            working_lock = ledger._lock
            try:
                ledger._lock = _FailingLock()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "lock acquisition failure",
                ):
                    ledger.verify_and_append_approved(
                        verifier=_StaticPlatformVerifier(
                            receipt.intent_payload_digest
                        ),
                        **arguments,
                    )
            finally:
                ledger._lock = working_lock
            self.assertIsNone(ledger.approval_state("approval-001"))
            self.assertEqual(ledger.head, stable_head)
            self.assertEqual(ledger.journal_path.stat().st_size, stable_size)
            self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

    def test_explicit_tmp_ledger_creates_secure_genesis_and_anchor(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            ledger_directory = Path(temporary_directory).resolve() / "approval-ledger"
            anchor = FakeIntegrityAnchorStore()

            ledger = ApprovalLedger(ledger_directory, anchor)

            self.assertEqual(
                stat.S_IMODE(ledger_directory.stat().st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(ledger.journal_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(ledger.head["sequence"], 0)
            self.assertEqual(anchor.state, ledger.head)
            self.assertEqual(anchor.mutation_count, 1)

    def test_approval_receipt_exactly_binds_proof_plan_operation_and_targets(self) -> None:
        receipt = _approval_receipt().as_dict()

        self.assertEqual(
            frozenset(receipt),
            frozenset(
                {
                    "approval_id",
                    "human_presence_proof_handle_ref",
                    "human_presence_issuer_id",
                    "human_presence_audience",
                    "plan_id",
                    "capability_id",
                    "job_id",
                    "attempt_id",
                    "lineage_id",
                    "canonical_operation_id",
                    "intent_payload_digest",
                    "trusted_envelope_digest",
                    "normalized_arguments_hash",
                    "local_uid",
                    "installation_id",
                    "signed_manifest_sha256",
                    "account_or_tenant_ref",
                    "target_ref",
                    "target_identity",
                    "input_fingerprints",
                    "record_revisions",
                    "nonce",
                    "issued_at",
                    "expires_at",
                    "issuer_channel",
                    "state",
                }
            ),
        )
        self.assertEqual(
            receipt["normalized_arguments_hash"],
            "6f9bf4eb900ec56706df0cfff779fe91090c3c8c3903f69d6afdedeb959c86d0",
        )
        envelope = _platform_chain()[1]
        self.assertEqual(
            receipt["intent_payload_digest"], envelope["intent_payload_digest"]
        )
        self.assertEqual(
            receipt["trusted_envelope_digest"], envelope["trusted_envelope_digest"]
        )
        self.assertEqual(receipt["state"], "approved")

    def test_approval_receipt_rejects_a_caller_supplied_fake_stage2_digest(self) -> None:
        proof, envelope, argv = _platform_chain()
        fake_envelope = deepcopy(envelope)
        fake_envelope["trusted_envelope_digest"] = "f" * 64
        with self.assertRaises(AgentTrustError) as raised:
            create_approval_receipt(
                verified_proof=proof,
                plan_id="plan-001",
                capability_id="COL-08",
                job_id="job-001",
                attempt_id="attempt-001",
                lineage_id="lineage-001",
                canonical_operation_id="cap.col-08",
                trusted_envelope=fake_envelope,
                normalized_argv_tokens=argv,
                signed_manifest_sha256="c" * 64,
                account_or_tenant_ref="tenant-001",
                target_ref="browser_session:dedicated",
                target_identity={"kind": "browser_session", "id": "dedicated"},
                input_fingerprints=["sha256:" + "d" * 64],
                record_revisions={"dedicated": 7},
                issuer_channel="platform_verified_human_presence",
            )

        self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
        self.assertEqual(
            raised.exception.reason_code,
            "APPROVAL_TRUSTED_ENVELOPE_INVALID",
        )

    def test_proof_burn_and_approved_append_are_one_single_use_journal_record(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            ledger_directory = Path(temporary_directory).resolve() / "approval-ledger"
            anchor = FakeIntegrityAnchorStore()
            ledger = ApprovalLedger(ledger_directory, anchor)
            receipt = _approval_receipt()

            _append_verified_approval(ledger, receipt=receipt)

            self.assertEqual(ledger.head["sequence"], 1)
            self.assertEqual(ledger.approval_state("approval-001"), "approved")
            self.assertEqual(anchor.mutation_count, 2)
            stable_head = ledger.head
            stable_size = ledger.journal_path.stat().st_size

            with self.assertRaises(AgentTrustError) as raised:
                _append_verified_approval(ledger, receipt=receipt)

            self.assertEqual(raised.exception.code, "APPROVAL_CONSUMED")
            self.assertEqual(ledger.head, stable_head)
            self.assertEqual(ledger.journal_path.stat().st_size, stable_size)
            self.assertEqual(anchor.mutation_count, 2)

            reopened = ApprovalLedger(ledger_directory, anchor)
            self.assertEqual(reopened.head, stable_head)
            self.assertEqual(reopened.approval_state("approval-001"), "approved")

    def test_platform_verification_and_approved_append_share_one_ledger_lock(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            catalog = load_capability_catalog()["catalog"]
            request = {"request_id": "req-001", "operation": "close"}
            argv = ["lulu", "collect", "browser", "close"]
            targets = [{"kind": "browser_session", "id": "dedicated"}]
            proposal = prepare_intent_proposal(
                catalog=catalog,
                producer="Agent",
                plan_id="plan-001",
                capability_id="COL-08",
                typed_request=request,
                normalized_argv_tokens=argv,
                typed_target_refs=targets,
                opaque_artifact_refs=[],
                account_ref="tenant-001",
                tenant_ref="tenant-001",
                record_and_resource_revisions={"dedicated": 7},
                idempotency_key="idem-001",
            )
            anchor = FakeIntegrityAnchorStore()
            ledger = ApprovalLedger(
                Path(temporary_directory).resolve() / "approval-ledger",
                anchor,
            )
            builder_calls = 0

            def receipt_builder(proof: dict[str, object]):
                nonlocal builder_calls
                builder_calls += 1
                envelope = construct_platform_human_presence_intent(
                    catalog=catalog,
                    capability_id="COL-08",
                    typed_request=request,
                    normalized_argv_tokens=argv,
                    typed_target_refs=targets,
                    opaque_artifact_refs=[],
                    account_ref="tenant-001",
                    tenant_ref="tenant-001",
                    record_and_resource_revisions={"dedicated": 7},
                    idempotency_key="idem-001",
                    proposal_digest=proposal["proposal_digest"],
                    verified_proof=proof,
                )
                return create_approval_receipt(
                    verified_proof=proof,
                    plan_id="plan-001",
                    capability_id="COL-08",
                    job_id="job-001",
                    attempt_id="attempt-001",
                    lineage_id="lineage-001",
                    canonical_operation_id="cap.col-08",
                    trusted_envelope=envelope,
                    normalized_argv_tokens=argv,
                    signed_manifest_sha256="c" * 64,
                    account_or_tenant_ref="tenant-001",
                    target_ref="browser_session:dedicated",
                    target_identity=targets[0],
                    input_fingerprints=["sha256:" + "d" * 64],
                    record_revisions={"dedicated": 7},
                    issuer_channel="platform_verified_human_presence",
                )

            arguments = {
                "handle_ref": "opaque-platform-handle",
                "verifier": _StaticPlatformVerifier(
                    proposal["intent_payload_digest"]
                ),
                "pinned_issuers": {
                    "platform-issuer": frozenset({"platform-key-v1"})
                },
                "expected_approval_id": "approval-001",
                "expected_intent_payload_digest": proposal[
                    "intent_payload_digest"
                ],
                "expected_local_uid": 501,
                "expected_installation_id": "install-001",
                "expected_nonce": "nonce-001",
                "now_wall": 1_776_864_060.0,
                "now_monotonic": 42_060.0,
                "receipt_builder": receipt_builder,
            }

            ledger.verify_and_append_approved(**arguments)
            self.assertEqual(builder_calls, 1)
            self.assertEqual(ledger.head["sequence"], 1)

            stable_head = ledger.head
            stable_size = ledger.journal_path.stat().st_size
            with self.assertRaises(AgentTrustError) as raised:
                ledger.verify_and_append_approved(**arguments)
            self.assertEqual(raised.exception.code, "APPROVAL_CONSUMED")
            self.assertEqual(builder_calls, 1)
            self.assertEqual(ledger.head, stable_head)
            self.assertEqual(ledger.journal_path.stat().st_size, stable_size)

    def test_concurrent_claim_writes_one_operation_receipt_and_one_lease(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            ledger = ApprovalLedger(
                Path(temporary_directory).resolve() / "approval-ledger",
                FakeIntegrityAnchorStore(),
            )
            _append_verified_approval(ledger)
            operation_receipt = create_operation_receipt(
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
                intent_payload_digest=_approval_receipt().intent_payload_digest,
                created_at="2026-08-20T08:00:30Z",
            )
            barrier = Barrier(3)
            successes: list[str] = []
            failures: list[str] = []

            def claim() -> None:
                barrier.wait()
                try:
                    ledger.claim(
                        approval_id="approval-001",
                        operation_receipt=operation_receipt,
                        job_revision=1,
                        lease_epoch=1,
                    )
                except AgentTrustError as exc:
                    failures.append(exc.code)
                else:
                    successes.append("claimed")

            threads = [Thread(target=claim), Thread(target=claim)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(successes, ["claimed"])
            self.assertEqual(failures, ["APPROVAL_CONSUMED"])
            self.assertEqual(ledger.head["sequence"], 2)
            record = ledger.approval_record("approval-001")
            self.assertEqual(record["state"], "claimed")
            self.assertEqual(record["job_revision"], 1)
            self.assertEqual(record["lease_epoch"], 1)
            self.assertEqual(
                record["operation_receipt"]["operation_receipt_id"],
                "operation-receipt-001",
            )

    def test_unknown_response_stays_claimed_until_a_determined_outcome(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            ledger = ApprovalLedger(
                Path(temporary_directory).resolve() / "approval-ledger",
                FakeIntegrityAnchorStore(),
            )
            _append_verified_approval(ledger)
            operation_receipt = create_operation_receipt(
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
                intent_payload_digest=_approval_receipt().intent_payload_digest,
                created_at="2026-08-20T08:00:30Z",
            )
            ledger.claim(
                approval_id="approval-001",
                operation_receipt=operation_receipt,
                job_revision=1,
                lease_epoch=1,
            )

            ledger.mark_recovery_required(
                approval_id="approval-001",
                reason_code="UNKNOWN_RESPONSE",
            )
            uncertain = ledger.approval_record("approval-001")
            self.assertEqual(uncertain["state"], "claimed")
            self.assertTrue(uncertain["recovery_required"])
            self.assertEqual(ledger.head["sequence"], 3)

            ledger.consume(
                approval_id="approval-001",
                outcome={"status": "known_failure", "code": "REMOTE_REJECTED"},
            )
            determined = ledger.approval_record("approval-001")
            self.assertEqual(determined["state"], "consumed")
            self.assertFalse(determined["recovery_required"])
            self.assertEqual(
                determined["outcome"],
                {"status": "known_failure", "code": "REMOTE_REJECTED"},
            )
            self.assertEqual(ledger.head["sequence"], 4)

    def test_integrity_fault_matrix_fails_closed_without_followup_mutation(self) -> None:
        actual_uid = os.getuid()

        with self.subTest(fault="symlink"):
            with TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory).resolve()
                real_directory = root / "real-ledger"
                real_directory.mkdir(mode=0o700)
                symlink_directory = root / "linked-ledger"
                symlink_directory.symlink_to(real_directory, target_is_directory=True)
                anchor = FakeIntegrityAnchorStore()
                with self.assertRaises(AgentTrustError) as raised:
                    ApprovalLedger(symlink_directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(anchor.mutation_count, 0)

        with self.subTest(fault="active_directory_mode"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                os.chmod(directory, 0o755)
                stable_size = ledger.journal_path.stat().st_size
                stable_anchor_mutations = anchor.mutation_count
                try:
                    with self.assertRaises(AgentTrustError) as raised:
                        _append_verified_approval(ledger)
                    self.assertEqual(raised.exception.exit_code, 60)
                    self.assertEqual(ledger.journal_path.stat().st_size, stable_size)
                    self.assertEqual(anchor.mutation_count, stable_anchor_mutations)
                finally:
                    os.chmod(directory, 0o700)

        with self.subTest(fault="owner"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ApprovalLedger(directory, anchor)
                stable_anchor_mutations = anchor.mutation_count
                with patch.object(ledger_module.os, "getuid", return_value=actual_uid + 1):
                    with self.assertRaises(AgentTrustError) as raised:
                        ApprovalLedger(directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with self.subTest(fault="truncate"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                _append_verified_approval(ledger)
                damaged = ledger.journal_path.read_bytes()[:-1]
                ledger.journal_path.write_bytes(damaged)
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(AgentTrustError) as raised:
                    ApprovalLedger(directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(ledger.journal_path.read_bytes(), damaged)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with self.subTest(fault="hmac_hash"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                _append_verified_approval(ledger)
                record = json.loads(ledger.journal_path.read_text(encoding="utf-8"))
                record["payload"]["approval_id"] = "approval-attacker"
                damaged = canonical_json_bytes(record) + b"\n"
                ledger.journal_path.write_bytes(damaged)
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(AgentTrustError) as raised:
                    ApprovalLedger(directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(ledger.journal_path.read_bytes(), damaged)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with self.subTest(fault="noncanonical_number"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                _append_verified_approval(ledger)
                record = json.loads(ledger.journal_path.read_text(encoding="utf-8"))
                record["payload"]["receipt"]["issued_at"] = float("nan")
                damaged = (
                    json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                    + b"\n"
                )
                ledger.journal_path.write_bytes(damaged)
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(AgentTrustError) as raised:
                    ApprovalLedger(directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(ledger.journal_path.read_bytes(), damaged)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with self.subTest(fault="high_water"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                _append_verified_approval(ledger)
                anchor.state = {
                    "epoch": anchor.state["epoch"],
                    "sequence": 0,
                    "head_hmac": "0" * 64,
                }
                stable_bytes = ledger.journal_path.read_bytes()
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(AgentTrustError) as raised:
                    ApprovalLedger(directory, anchor)
                self.assertEqual(raised.exception.exit_code, 60)
                self.assertEqual(ledger.journal_path.read_bytes(), stable_bytes)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)

        with self.subTest(fault="anchor_crash_window"):
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory).resolve() / "approval-ledger"
                anchor = FakeIntegrityAnchorStore()
                ledger = ApprovalLedger(directory, anchor)
                anchor.fail_next_compare_and_set = True
                with self.assertRaises(AgentTrustError) as append_failure:
                    _append_verified_approval(ledger)
                self.assertEqual(append_failure.exception.exit_code, 60)
                crash_bytes = ledger.journal_path.read_bytes()
                stable_anchor_mutations = anchor.mutation_count
                with self.assertRaises(AgentTrustError) as reopen_failure:
                    ApprovalLedger(directory, anchor)
                self.assertEqual(reopen_failure.exception.exit_code, 60)
                self.assertEqual(ledger.journal_path.read_bytes(), crash_bytes)
                self.assertEqual(anchor.mutation_count, stable_anchor_mutations)


if __name__ == "__main__":
    unittest.main()
