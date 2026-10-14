from __future__ import annotations

import unittest

from src.core.models import Segment, Transcript


class _FakeJobBackend:
    def __init__(self) -> None:
        self.mutations: list[str] = []

    def start(self, *, plan, claims, operation_receipt, approval_claim):
        del approval_claim
        self.mutations.append("job:start")
        self.plan = plan
        self.claims = claims
        self.operation_receipt = operation_receipt
        return {"job_revision": 3, "lease_epoch": 1}

    def complete_success(self, *, claims, result_ref):
        self.mutations.append("job:succeeded")
        return {
            "job_id": claims.job_id,
            "attempt_id": claims.attempt_id,
            "lineage_id": claims.lineage_id,
            "job_revision": 4,
            "lease_epoch": 1,
            "state": "succeeded",
            "result_ref": result_ref,
        }

    def complete_failure(self, *, claims, reason_code):
        self.mutations.append(f"job:failed:{reason_code}")

    def mark_recovery_required(self, *, claims, reason_code):
        self.mutations.append(f"job:recovery_required:{reason_code}")


class CliUrlApprovalTests(unittest.TestCase):
    def test_url_run_without_core_receipt_has_zero_authority_or_effect_mutation(self) -> None:
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )

        mutations: list[str] = []
        service = LinkTranscriptionApplicationService(
            installation_id="installation-test-001",
            pipeline_runner=lambda **_kwargs: mutations.append("pipeline"),
            job_backend=lambda *_args, **_kwargs: mutations.append("job"),
            result_writer=lambda *_args, **_kwargs: mutations.append("result"),
        )

        with self.assertRaises(LinkTranscriptionError) as raised:
            service.run("https://example.com/watch?v=42", approval_receipt=None)

        self.assertEqual(raised.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual(mutations, [])

    def test_verified_host_receipt_exactly_binds_and_runs_pipeline_once(self) -> None:
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
        )
        from tests.test_cli_url_host_bridge import approved_host_broker

        pipeline_requests = []
        job_backend = _FakeJobBackend()

        def pipeline_runner(*, request, progress, is_cancelled, cover_workflow=None):
            del progress, cover_workflow
            pipeline_requests.append(request)
            self.assertFalse(is_cancelled())
            return Transcript(
                text="链接转录完成。",
                segments=[Segment("链接转录完成。", 0.0, 1.0)],
                language="zh",
                engine="local_sensevoice",
                source=request.source,
                title="公开作品",
                meta={"platform": "example"},
                saved_media=None,
            )

        service = LinkTranscriptionApplicationService(
            installation_id="installation-test-001",
            pipeline_runner=pipeline_runner,
            job_backend=job_backend,
            result_writer=lambda job_id, _result: {
                "result_ref": f"lulu-result:{job_id}:sha256:" + "a" * 64,
                "content_hash": "a" * 64,
            },
        )
        plan = service.prepare(
            "https://example.com/watch?v=42",
            prefer_subtitles=True,
            save_media=True,
            media_mode="video",
            media_directory="/tmp/lulu-media",
            save_cover=True,
        )
        with approved_host_broker(
            installation_id="installation-test-001",
            now=1_777_000_000.0,
            ids={
                "job": "job-link-001",
                "attempt": "attempt-link-001",
                "lineage": "lineage-link-001",
                "idempotency": "idem-link-001",
            },
        ) as host:
            receipt = host.request_approval(plan=plan).receipt

        result = service.run(
            "https://example.com/watch?v=42",
            approval_receipt=receipt,
            prefer_subtitles=True,
            save_media=True,
            media_mode="video",
            media_directory="/tmp/lulu-media",
            save_cover=True,
            now=1_777_000_001.0,
        )

        self.assertEqual(len(pipeline_requests), 1)
        self.assertEqual(pipeline_requests[0].media_mode, "video")
        self.assertEqual(job_backend.mutations, ["job:start", "job:succeeded"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["job_receipt"]["job_id"], "job-link-001")
        self.assertEqual(
            result["operation_receipt"]["approval_profile"],
            "APPROVAL_HUMAN_RECEIPT",
        )

        with self.assertRaises(Exception) as replayed:
            service.run(
                "https://example.com/watch?v=42",
                approval_receipt=receipt,
                prefer_subtitles=True,
                save_media=True,
                media_mode="video",
                media_directory="/tmp/lulu-media",
                save_cover=True,
                now=1_777_000_002.0,
            )
        self.assertEqual(replayed.exception.code, "APPROVAL_CONSUMED")
        self.assertEqual(len(pipeline_requests), 1)

    def test_plain_claim_plan_drift_and_expiry_fail_before_any_mutation(self) -> None:
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )
        from tests.test_cli_url_host_bridge import approved_host_broker

        job_backend = _FakeJobBackend()
        pipeline_calls: list[str] = []
        service = LinkTranscriptionApplicationService(
            installation_id="installation-test-hostile",
            pipeline_runner=lambda **_kwargs: pipeline_calls.append("pipeline"),
            job_backend=job_backend,
            result_writer=lambda *_args: {},
        )
        plan = service.prepare("https://example.com/video/7")
        with self.assertRaises(LinkTranscriptionError) as plain:
            service.run(
                "https://example.com/video/7",
                approval_receipt={"approved": True, "text": "OK"},
                now=1_777_100_001.0,
            )
        self.assertEqual(plain.exception.code, "APPROVAL_INVALID")

        with approved_host_broker(
            installation_id="installation-test-hostile",
            now=1_777_100_000.0,
        ) as host:
            drift_receipt = host.request_approval(plan=plan).receipt
            expired_receipt = host.request_approval(plan=plan).receipt
        with self.assertRaises(LinkTranscriptionError) as drift:
            service.run(
                "https://example.com/video/7",
                approval_receipt=drift_receipt,
                prefer_subtitles=True,
                now=1_777_100_001.0,
            )
        self.assertEqual(drift.exception.code, "APPROVAL_INVALID")
        with self.assertRaises(LinkTranscriptionError) as expired:
            service.run(
                "https://example.com/video/7",
                approval_receipt=expired_receipt,
                now=1_777_100_121.0,
            )
        self.assertEqual(expired.exception.code, "APPROVAL_EXPIRED")
        self.assertEqual(job_backend.mutations, [])
        self.assertEqual(pipeline_calls, [])


if __name__ == "__main__":
    unittest.main()
