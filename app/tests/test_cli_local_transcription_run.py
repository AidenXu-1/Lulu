from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard
from src.core.job_ledger.store import JobLedgerStore
from src.core.models import Segment, Transcript
from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_application import LocalFileTranscriptionApplicationService
from src.core.transcription_result_store import TranscriptionResultStore
from src.core.transcription_result_store import TranscriptionResultStoreError
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class _FreshCachedLicense:
    def __init__(self) -> None:
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return SimpleNamespace(allowed=True, code="cached_allowed")


class CliLocalTranscriptionRunTests(unittest.TestCase):
    def test_run_executes_foreground_pipeline_and_commits_job_receipt(self) -> None:
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        with TemporaryDirectory(prefix="lulu-cli-run-") as directory:
            root = Path(directory).resolve()
            source = root / "source.wav"
            source.write_bytes(b"RIFF-foreground-pipeline")
            license_service = _FreshCachedLicense()
            guard = UnifiedLicenseAuthGuard(license_service)
            model_calls: list[str] = []

            def ready(engine: str) -> bool:
                model_calls.append(engine)
                return True

            def pipeline(source_path: str, *, engine_name, progress, is_cancelled):
                self.assertEqual(source_path, str(source))
                self.assertEqual(engine_name, "local_sensevoice")
                self.assertFalse(is_cancelled())
                return Transcript(
                    text="本地转录完成。",
                    segments=[Segment("本地转录完成。", 0.0, 1.5)],
                    language="zh",
                    engine="local_sensevoice",
                    source=source_path,
                    title=None,
                    meta={},
                    saved_media=None,
                )

            service = LocalFileTranscriptionApplicationService(
                guard,
                pipeline_runner=pipeline,
                model_readiness_probe=ready,
            )
            application_support = root / "Application Support"
            application_support.mkdir(mode=0o700)
            layout = SharedProductStorageLayout.from_application_support_root(
                application_support
            )
            layout.product_root.mkdir(mode=0o700)
            result_store = TranscriptionResultStore(layout)
            job_store = JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore())
            ids = iter(("job-run-001", "attempt-run-001", "lineage-run-001"))
            times = iter(
                f"2026-08-21T01:00:0{second}Z" for second in range(8)
            )
            application = LocalTranscriptionJobApplication(
                runtime_verification={
                    "profile": "trusted_test",
                    "verified": True,
                    "signature_kind": "developer_id",
                    "installation_id": "installation-test-001",
                },
                authorization_guard=guard,
                model_readiness_probe=ready,
                transcription_service=service,
                job_store=job_store,
                result_store=result_store,
                id_factory=lambda _kind: next(ids),
                now=lambda: next(times),
            )
            stdout = StringIO()
            stderr = StringIO()

            exit_code = main(
                ["transcribe", "run", str(source), "--output", "json"],
                stdout=stdout,
                stderr=stderr,
                product_identity={"adapter_id": "agent_cli"},
                local_transcription_runtime=LocalTranscriptionCliRuntime(application),
            )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            envelope = json.loads(stdout.getvalue())
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "transcribe.run")
            result = envelope["result"]
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(
                result["result_ref"].startswith(
                    "lulu-result:job-run-001:sha256:"
                )
            )
            self.assertRegex(result["content_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["job_receipt"], {
                "job_id": "job-run-001",
                "job_revision": 4,
                "state": "succeeded",
                "last_sequence": 4,
                "result_ref": result["result_ref"],
                "receipt_ref": "lulu-job:job-run-001:revision:4",
            })
            stored = job_store.get_job("job-run-001")
            self.assertEqual(stored.state.value, "succeeded")
            self.assertEqual(stored.result_ref, result["result_ref"])
            self.assertEqual(
                result_store.read("job-run-001")["transcript"]["text"],
                "本地转录完成。",
            )
            self.assertGreaterEqual(license_service.calls, 2)
            self.assertGreaterEqual(len(model_calls), 2)

    def test_repeated_run_reports_existing_idempotent_job_without_new_id_mutation(self) -> None:
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
            LocalTranscriptionJobError,
        )

        with TemporaryDirectory(prefix="lulu-cli-run-idempotent-") as directory:
            root = Path(directory).resolve()
            source = root / "source.wav"
            source.write_bytes(b"RIFF-idempotent-pipeline")
            guard = UnifiedLicenseAuthGuard(_FreshCachedLicense())
            pipeline_calls: list[str] = []

            def pipeline(source_path: str, **_kwargs):
                pipeline_calls.append(source_path)
                return Transcript(
                    text="只执行一次。",
                    segments=[Segment("只执行一次。", 0.0, 1.0)],
                    language="zh",
                    engine="local_sensevoice",
                    source=source_path,
                    title=None,
                    meta={},
                    saved_media=None,
                )

            service = LocalFileTranscriptionApplicationService(
                guard,
                pipeline_runner=pipeline,
                model_readiness_probe=lambda _engine: True,
            )
            application_support = root / "Application Support"
            application_support.mkdir(mode=0o700)
            layout = SharedProductStorageLayout.from_application_support_root(
                application_support
            )
            layout.product_root.mkdir(mode=0o700)
            store = JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore())
            ids = iter(
                (
                    "job-first",
                    "attempt-first",
                    "lineage-first",
                    "job-second",
                    "attempt-second",
                    "lineage-second",
                )
            )
            clock = iter(
                f"2026-08-21T03:00:{second:02d}Z" for second in range(20)
            )
            application = LocalTranscriptionJobApplication(
                runtime_verification={
                    "profile": "trusted_test",
                    "verified": True,
                    "signature_kind": "developer_id",
                    "installation_id": "installation-test-001",
                },
                authorization_guard=guard,
                model_readiness_probe=lambda _engine: True,
                transcription_service=service,
                job_store=store,
                result_store=TranscriptionResultStore(layout),
                id_factory=lambda _kind: next(ids),
                now=lambda: next(clock),
            )

            first = application.run(str(source))
            self.assertEqual(first["status"], "succeeded")
            with self.assertRaises(LocalTranscriptionJobError) as raised:
                application.run(str(source))

            self.assertEqual(raised.exception.code, "CONFLICT")
            self.assertEqual(
                raised.exception.reason_code,
                "IDEMPOTENT_JOB_ALREADY_EXISTS",
            )
            self.assertEqual(pipeline_calls, [str(source)])
            self.assertEqual([job.job_id for job in store.list_jobs()], ["job-first"])

    def test_result_write_failure_terminalizes_claimed_job(self) -> None:
        from src.core.job_ledger.contracts import JobState
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        class _FailingResultStore:
            def write(self, _job_id: str, _result: object):
                raise TranscriptionResultStoreError("结果目录权限不安全")

        with TemporaryDirectory(prefix="lulu-cli-run-failure-") as directory:
            root = Path(directory).resolve()
            source = root / "source.wav"
            source.write_bytes(b"RIFF-failing-result-store")
            guard = UnifiedLicenseAuthGuard(_FreshCachedLicense())

            def pipeline(source_path: str, **_kwargs):
                return Transcript(
                    text="不会落盘。",
                    segments=[Segment("不会落盘。", 0.0, 1.0)],
                    language="zh",
                    engine="local_sensevoice",
                    source=source_path,
                    title=None,
                    meta={},
                    saved_media=None,
                )

            service = LocalFileTranscriptionApplicationService(
                guard,
                pipeline_runner=pipeline,
                model_readiness_probe=lambda _engine: True,
            )
            application_support = root / "Application Support"
            application_support.mkdir(mode=0o700)
            layout = SharedProductStorageLayout.from_application_support_root(
                application_support
            )
            layout.product_root.mkdir(mode=0o700)
            store = JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore())
            ids = iter(("job-failed", "attempt-failed", "lineage-failed"))
            clock = iter(
                f"2026-08-21T04:00:{second:02d}Z" for second in range(12)
            )
            application = LocalTranscriptionJobApplication(
                runtime_verification={
                    "profile": "trusted_test",
                    "verified": True,
                    "signature_kind": "developer_id",
                    "installation_id": "installation-test-001",
                },
                authorization_guard=guard,
                model_readiness_probe=lambda _engine: True,
                transcription_service=service,
                job_store=store,
                result_store=_FailingResultStore(),
                id_factory=lambda _kind: next(ids),
                now=lambda: next(clock),
            )

            with self.assertRaises(TranscriptionResultStoreError):
                application.run(str(source))

            failed = store.get_job("job-failed")
            self.assertEqual(failed.state, JobState.FAILED)
            self.assertIsNone(failed.result_ref)


if __name__ == "__main__":
    unittest.main()
