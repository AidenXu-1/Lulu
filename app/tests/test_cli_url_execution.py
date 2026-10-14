from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.core.models import Segment, Transcript


class _NoEffectBackend:
    def __init__(self) -> None:
        self.mutations: list[str] = []

    def start(self, **_kwargs):
        self.mutations.append("start")
        return {"job_revision": 3, "lease_epoch": 1}

    def complete_success(self, **_kwargs):
        self.mutations.append("success")
        return {"state": "succeeded"}

    def complete_failure(self, **_kwargs):
        self.mutations.append("failure")

    def mark_recovery_required(self, **_kwargs):
        self.mutations.append("recovery")


class CliUrlExecutionTests(unittest.TestCase):
    def test_pipeline_adapter_reuses_existing_link_subtitle_media_cover_seam(self) -> None:
        from src.core.link_transcription_application import (
            run_link_transcription_pipeline_adapter,
        )
        from src.core.link_transcription_contracts import LinkTranscriptionRequestV1

        request = LinkTranscriptionRequestV1(
            source="https://example.com/video/adapter",
            prefer_subtitles=True,
            subtitle_fallback="fail",
            save_media=True,
            media_mode="video",
            media_directory="/tmp/lulu-adapter-media",
            save_cover=True,
        )
        cover_workflow = object()
        sentinel = object()
        with patch(
            "src.core.pipeline.transcribe_source", return_value=sentinel
        ) as existing_pipeline:
            result = run_link_transcription_pipeline_adapter(
                request=request,
                progress=lambda _message, _fraction: None,
                is_cancelled=lambda: False,
                cover_workflow=cover_workflow,
            )

        self.assertIs(result, sentinel)
        existing_pipeline.assert_called_once_with(
            request.source,
            engine_name="local_sensevoice",
            enable_link_download=True,
            keep_media=True,
            media_dir="/tmp/lulu-adapter-media",
            link_media_mode="video",
            prefer_subtitles=True,
            subtitle_fallback="fail",
            progress=existing_pipeline.call_args.kwargs["progress"],
            is_cancelled=existing_pipeline.call_args.kwargs["is_cancelled"],
            cover_snapshot=existing_pipeline.call_args.kwargs["cover_snapshot"],
            cover_workflow=cover_workflow,
        )

    def test_cli_run_without_same_bundle_broker_is_human_action_required(self) -> None:
        from src.agent_cli.main import main
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
        )

        pipeline_calls: list[str] = []
        backend = _NoEffectBackend()
        service = LinkTranscriptionApplicationService(
            installation_id="installation-cli-url-001",
            pipeline_runner=lambda **_kwargs: pipeline_calls.append("pipeline"),
            job_backend=backend,
            result_writer=lambda *_args: {},
        )
        stdout = StringIO()
        stderr = StringIO()

        exit_code = main(
            [
                "transcribe",
                "run",
                "https://example.com/video/9",
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=stderr,
            link_transcription_service=service,
        )

        self.assertEqual(exit_code, 11)
        self.assertEqual(stderr.getvalue(), "")
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["error"]["code"], "HUMAN_ACTION_REQUIRED")
        self.assertEqual(pipeline_calls, [])
        self.assertEqual(backend.mutations, [])

    def test_real_job_ledger_persists_exact_network_operation_receipt(self) -> None:
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionJobBackend,
        )
        from tests.test_cli_url_host_bridge import approved_host_broker
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore

        with TemporaryDirectory(prefix="lulu-url-ledger-") as directory:
            clock = iter(
                f"2026-08-21T12:00:{second:02d}Z" for second in range(20)
            )
            store = JobLedgerStore(
                Path(directory).resolve() / "jobs",
                FakeIntegrityAnchorStore(),
            )
            service = LinkTranscriptionApplicationService(
                installation_id="installation-ledger-001",
                pipeline_runner=lambda *, request, **_kwargs: Transcript(
                    text="权威任务完成。",
                    segments=[Segment("权威任务完成。", 0.0, 1.0)],
                    language="zh",
                    engine="local_sensevoice",
                    source=request.source,
                ),
                job_backend=LinkTranscriptionJobBackend(
                    store, now=lambda: next(clock)
                ),
                result_writer=lambda job_id, _result: {
                    "result_ref": f"lulu-result:{job_id}:sha256:" + "b" * 64,
                    "content_hash": "b" * 64,
                },
            )
            plan = service.prepare("https://example.com/video/ledger")
            with approved_host_broker(
                installation_id="installation-ledger-001",
                now=1_777_300_000.0,
                ids={
                    "job": "job-link-ledger-001",
                    "attempt": "attempt-link-ledger-001",
                    "lineage": "lineage-link-ledger-001",
                    "idempotency": "idem-link-ledger-001",
                },
            ) as host:
                receipt = host.request_approval(plan=plan).receipt

            result = service.run(
                "https://example.com/video/ledger",
                approval_receipt=receipt,
                now=1_777_300_001.0,
            )

            persisted = store.get_job("job-link-ledger-001")
            operation = persisted.attempts[-1].operation_receipt
            self.assertEqual(persisted.state.value, "succeeded")
            self.assertEqual(result["job_receipt"]["job_revision"], 4)
            self.assertIsNotNone(operation)
            self.assertEqual(operation.job_profile, "JOB_NETWORK")
            self.assertEqual(operation.effect_profile, "EFFECT_NETWORK_DOWNLOAD")
            self.assertEqual(operation.idempotency_key, "idem-link-ledger-001")
            self.assertEqual(operation.intent_payload_digest, plan["plan_digest"])


if __name__ == "__main__":
    unittest.main()
