"""Phase 5 authoritative job-ledger persistence tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import fcntl
import importlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from src.core.job_ledger.contracts import (
    JobLedgerError,
    JobState,
    MigrationReceiptV1,
    OperationReceiptV1,
)


class FakeIntegrityAnchorStore:
    def __init__(self) -> None:
        self.state: dict[str, object] | None = None
        self.mutation_count = 0

    def hmac_key(self) -> bytes:
        return b"phase5-job-ledger-test-hmac-key!"

    def load(self) -> dict[str, object] | None:
        return deepcopy(self.state)

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None:
        if self.state != expected:
            raise RuntimeError("anchor changed")
        self.state = deepcopy(replacement)
        self.mutation_count += 1


class RejectNextIntegrityAnchorStore(FakeIntegrityAnchorStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_next = False

    def compare_and_set(self, expected, replacement) -> None:
        if self.reject_next:
            self.reject_next = False
            raise RuntimeError("simulated anchor CAS rejection")
        super().compare_and_set(expected, replacement)


def _create_arguments() -> dict[str, object]:
    return {
        "job_id": "job-001",
        "attempt_id": "attempt-001",
        "lineage_id": "lineage-001",
        "job_kind": "transcription",
        "input_ref": "source:sha256:" + "a" * 64,
        "frozen_policy": {"engine": "local_sensevoice", "mode": "offline"},
        "created_by": "phase5-test",
        "installation_id": "installation-001",
        "canonical_operation_id": "OP_TRANSCRIBE_LOCAL_V1",
        "idempotency_key": "idem-001",
        "intent_payload_digest": "b" * 64,
        "created_at": "2026-08-20T01:00:00Z",
        "effect_fingerprint": "effect:local-read",
        "account_ref": "account:none",
        "target_ref": "target:transcript:test",
        "uncertain_response_state": "none",
    }


def _operation_receipt(receipt_id: str) -> OperationReceiptV1:
    return OperationReceiptV1(
        operation_receipt_id=receipt_id,
        job_id="job-001",
        attempt_id="attempt-001",
        lineage_id="lineage-001",
        installation_id="installation-001",
        canonical_operation_id="OP_TRANSCRIBE_LOCAL_V1",
        effect_profile="EFFECT_LOCAL_READ",
        job_profile="JOB_LOCAL",
        approval_profile="APPROVAL_NONE",
        idempotency_key="idem-001",
        intent_payload_digest="b" * 64,
        created_at="2026-08-20T01:00:02Z",
    )


if __name__ == "__main__":
    unittest.main()
