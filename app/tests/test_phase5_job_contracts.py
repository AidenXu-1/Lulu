"""Phase 5 closed job-ledger DTO contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import importlib
import sys
import unittest


class Phase5JobContractTests(unittest.TestCase):
    def test_closed_non_qt_job_contracts_are_frozen_and_secret_free(self) -> None:
        before_qt = {name for name in sys.modules if name.startswith("PySide6")}
        try:
            contracts = importlib.import_module("src.core.job_ledger.contracts")
        except ModuleNotFoundError as exc:
            self.fail(f"Phase 5 job contracts are unavailable: {exc}")

        self.assertEqual(
            {state.value for state in contracts.JobState},
            {
                "prepared",
                "queued",
                "running",
                "needs_input",
                "cancel_requested",
                "recovery_required",
                "succeeded",
                "partial",
                "failed",
                "cancelled",
            },
        )
        self.assertEqual(
            {state.value for state in contracts.TERMINAL_STATES},
            {"succeeded", "partial", "failed", "cancelled"},
        )
        self.assertEqual(contracts.JOB_LEDGER_SCHEMA, "lulu.job-ledger/v1")
        self.assertEqual(contracts.JOB_LEDGER_SCHEMA_VERSION, 1)

        self.assertEqual(
            tuple(field.name for field in fields(contracts.OperationReceiptV1)),
            (
                "operation_receipt_id",
                "job_id",
                "attempt_id",
                "lineage_id",
                "installation_id",
                "canonical_operation_id",
                "effect_profile",
                "job_profile",
                "approval_profile",
                "idempotency_key",
                "intent_payload_digest",
                "created_at",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.AttemptV1)),
            (
                "attempt_id",
                "lineage_id",
                "attempt_number",
                "state",
                "lease_epoch",
                "worker_instance_id",
                "operation_receipt",
                "effect_fingerprint",
                "account_ref",
                "target_ref",
                "uncertain_response_state",
                "created_at",
                "updated_at",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(contracts.JobV1)),
            (
                "job_id",
                "job_kind",
                "input_ref",
                "frozen_policy",
                "created_by",
                "installation_id",
                "canonical_operation_id",
                "idempotency_key",
                "intent_payload_digest",
                "state",
                "job_revision",
                "event_sequence",
                "current_attempt_id",
                "lineage_id",
                "created_at",
                "updated_at",
                "result_ref",
                "partial_failures",
                "attempts",
            ),
        )
        forbidden_fragments = ("secret", "token", "api_key", "license_code")
        for dto in (
            contracts.JobV1,
            contracts.AttemptV1,
            contracts.EventV1,
            contracts.ResultV1,
            contracts.OperationReceiptV1,
        ):
            names = tuple(field.name.lower() for field in fields(dto))
            self.assertFalse(
                any(fragment in name for name in names for fragment in forbidden_fragments)
            )

        receipt = contracts.OperationReceiptV1(
            operation_receipt_id="receipt-001",
            job_id="job-001",
            attempt_id="attempt-001",
            lineage_id="lineage-001",
            installation_id="installation-001",
            canonical_operation_id="OP_TEST_V1",
            effect_profile="EFFECT_LOCAL_READ",
            job_profile="JOB_LOCAL",
            approval_profile="APPROVAL_NONE",
            idempotency_key="idem-001",
            intent_payload_digest="a" * 64,
            created_at="2026-08-20T00:00:00Z",
        )
        attempt = contracts.AttemptV1(
            attempt_id="attempt-001",
            lineage_id="lineage-001",
            attempt_number=1,
            state=contracts.JobState.PREPARED,
            lease_epoch=0,
            worker_instance_id=None,
            operation_receipt=receipt,
            effect_fingerprint="effect:local-read",
            account_ref="account:none",
            target_ref="target:test",
            uncertain_response_state="none",
            created_at="2026-08-20T00:00:00Z",
            updated_at="2026-08-20T00:00:00Z",
        )
        job = contracts.JobV1(
            job_id="job-001",
            job_kind="test",
            input_ref="input:sha256:" + "b" * 64,
            frozen_policy={"mode": "offline"},
            created_by="test",
            installation_id="installation-001",
            canonical_operation_id="OP_TEST_V1",
            idempotency_key="idem-001",
            intent_payload_digest="a" * 64,
            state=contracts.JobState.PREPARED,
            job_revision=1,
            event_sequence=1,
            current_attempt_id="attempt-001",
            lineage_id="lineage-001",
            created_at="2026-08-20T00:00:00Z",
            updated_at="2026-08-20T00:00:00Z",
            result_ref=None,
            partial_failures=(),
            attempts=(attempt,),
        )
        with self.assertRaises(FrozenInstanceError):
            job.state = contracts.JobState.RUNNING
        self.assertEqual(job.as_dict()["state"], "prepared")
        self.assertEqual(
            {name for name in sys.modules if name.startswith("PySide6")},
            before_qt,
        )


if __name__ == "__main__":
    unittest.main()
