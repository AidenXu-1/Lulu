from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard
from src.core.job_ledger.contracts import JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments


class _FreshCachedLicense:
    def __init__(self) -> None:
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return SimpleNamespace(allowed=True, code="cached_allowed")


class CliLocalTranscriptionJobsTests(unittest.TestCase):
    def test_jobs_replay_authoritative_events_and_keep_cancel_request_distinct(self) -> None:
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        with TemporaryDirectory(prefix="lulu-cli-jobs-") as directory:
            root = Path(directory).resolve()
            store = JobLedgerStore(root / "shared-jobs", FakeIntegrityAnchorStore())
            created = store.create_job(**_create_arguments())
            queued = store.transition(
                created.job_id,
                expected_revision=created.job_revision,
                new_state=JobState.QUEUED,
                changed_at="2026-08-21T02:00:01Z",
            )
            license_service = _FreshCachedLicense()
            application = LocalTranscriptionJobApplication(
                runtime_verification={
                    "profile": "trusted_test",
                    "verified": True,
                    "signature_kind": "developer_id",
                    "installation_id": "installation-001",
                },
                authorization_guard=UnifiedLicenseAuthGuard(license_service),
                model_readiness_probe=lambda _engine: True,
            )
            runtime = LocalTranscriptionCliRuntime(
                application,
                job_control=JobControlService(store),
                now=lambda: "2026-08-21T02:00:02Z",
            )

            def invoke(argv: list[str]) -> tuple[int, str, str]:
                stdout = StringIO()
                stderr = StringIO()
                code = main(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    product_identity={"adapter_id": "agent_cli"},
                    local_transcription_runtime=runtime,
                )
                return code, stdout.getvalue(), stderr.getvalue()

            code, raw, error = invoke(["jobs", "list", "--output", "json"])
            self.assertEqual((code, error), (0, ""))
            listed = json.loads(raw)["result"]["jobs"]
            self.assertEqual([item["job_id"] for item in listed], ["job-001"])
            self.assertEqual(listed[0]["state"], "queued")

            code, raw, error = invoke(
                ["jobs", "show", "--job-id", "job-001", "--output", "json"]
            )
            self.assertEqual((code, error), (0, ""))
            shown = json.loads(raw)["result"]
            self.assertEqual(shown["job_id"], "job-001")
            self.assertEqual(shown["job_revision"], queued.job_revision)

            code, raw, error = invoke(
                [
                    "jobs",
                    "wait",
                    "--job-id",
                    "job-001",
                    "--after-sequence",
                    "0",
                    "--output",
                    "jsonl",
                ]
            )
            self.assertEqual((code, error), (0, ""))
            events = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(
                [event["payload"]["state"] for event in events],
                ["prepared", "queued"],
            )

            code, raw, error = invoke(
                ["jobs", "receipt", "--job-id", "job-001", "--output", "json"]
            )
            self.assertEqual((code, error), (0, ""))
            receipt = json.loads(raw)["result"]
            self.assertEqual(receipt["state"], "queued")
            self.assertEqual(receipt["partial_failures"], [])

            code, raw, error = invoke(
                ["jobs", "cancel", "--job-id", "job-001", "--output", "json"]
            )
            self.assertEqual((code, error), (0, ""))
            cancellation = json.loads(raw)["result"]
            self.assertEqual(cancellation["state"], "cancel_requested")
            self.assertEqual(cancellation["cancellation"], "requested")
            self.assertEqual(store.get_job("job-001").state, JobState.CANCEL_REQUESTED)
            self.assertGreaterEqual(license_service.calls, 5)


if __name__ == "__main__":
    unittest.main()
