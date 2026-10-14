"""Phase 4 operation receipt binding contract tests."""

from __future__ import annotations

import unittest

from src.core.agent_trust.approval import (
    assert_authoritative_operation_binding,
    create_operation_receipt,
)
from src.core.agent_trust.contracts import AgentTrustError


def _operation_receipt():
    return create_operation_receipt(
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


class _FakeAuthoritativeOperationStore:
    def __init__(self) -> None:
        self.mutation_count = 0

    def load_authoritative_binding(self, receipt_id: str) -> dict[str, object]:
        if receipt_id != "operation-receipt-001":
            raise KeyError(receipt_id)
        return {
            "receipt": _operation_receipt(),
            "job": {"job_id": "job-001"},
            "attempt": {
                "attempt_id": "attempt-001",
                "lineage_id": "lineage-001",
                "canonical_operation_id": "cap.col-08",
            },
        }


class OperationReceiptBindingTests(unittest.TestCase):
    def test_operation_receipt_has_only_the_twelve_immutable_binding_fields(self) -> None:
        receipt = _operation_receipt().as_dict()

        self.assertEqual(
            tuple(receipt),
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
        self.assertEqual(receipt["job_id"], "job-001")
        self.assertEqual(receipt["attempt_id"], "attempt-001")
        self.assertEqual(receipt["lineage_id"], "lineage-001")
        self.assertEqual(receipt["installation_id"], "install-001")
        self.assertEqual(receipt["canonical_operation_id"], "cap.col-08")

    def test_cross_job_assertion_fails_before_authoritative_store_mutation(self) -> None:
        store = _FakeAuthoritativeOperationStore()

        with self.assertRaises(AgentTrustError) as raised:
            assert_authoritative_operation_binding(
                store=store,
                operation_receipt_id="operation-receipt-001",
                assertions={
                    "job_id": "job-attacker",
                    "expected_attempt_id": "attempt-001",
                    "expected_lineage_id": "lineage-001",
                    "expected_installation_id": "install-001",
                    "expected_canonical_operation_id": "cap.col-08",
                    "effect_profile": "EFFECT_LOCAL_WRITE",
                    "job_profile": "JOB_LOCAL",
                    "approval_profile": "APPROVAL_HUMAN_RECEIPT",
                    "idempotency_key": "idem-001",
                },
                current_installation_id="install-001",
                authoritative_intent_payload_digest="a" * 64,
            )

        self.assertEqual(raised.exception.code, "INTEGRITY_ERROR")
        self.assertEqual(
            raised.exception.reason_code,
            "OPERATION_BINDING_MISMATCH",
        )
        self.assertEqual(store.mutation_count, 0)


if __name__ == "__main__":
    unittest.main()
