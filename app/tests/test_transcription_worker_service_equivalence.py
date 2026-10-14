from __future__ import annotations

import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.models import Transcript
from src.ui.worker import TranscribeWorker

from tests.test_transcription_application_service import (
    EXPECTED_PROGRESS,
    _always_ready_model_probe,
    _deterministic_pipeline_runner,
    _load_phase1a_modules,
    _reset_phase1a_stub_calls,
    _transcript_fingerprint,
    _unified_guard,
    _write_phase1a_fixture,
)


class Phase1AWorkerEquivalenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lulu-phase1a-worker-")
        self.addCleanup(self._tmp.cleanup)
        self.source_path = _write_phase1a_fixture(self._tmp.name)
        _reset_phase1a_stub_calls()

    def _service(self, *, guard=None, runner=None, readiness=None):
        contracts, application = _load_phase1a_modules()
        if guard is None:
            guard = _unified_guard()
        service = application.LocalFileTranscriptionApplicationService(
            guard,
            pipeline_runner=runner or _deterministic_pipeline_runner,
            model_readiness_probe=readiness or _always_ready_model_probe,
        )
        return contracts, application, guard, service

    def _worker(self, guard, service, *, source_path=None, engine_name="local_sensevoice"):
        return TranscribeWorker(
            source_path or self.source_path,
            engine_name,
            True,
            execution_policy=None,
            local_file_transcription_authorization_guard=guard,
            local_file_transcription_service=service,
        )

    @staticmethod
    def _capture(worker):
        events = {
            "progress": [],
            "finished_ok": [],
            "failed": [],
            "cancelled": [],
        }
        worker.progress.connect(lambda *event: events["progress"].append(event))
        worker.finished_ok.connect(lambda result: events["finished_ok"].append(result))
        worker.failed.connect(lambda message: events["failed"].append(message))
        worker.cancelled.connect(lambda: events["cancelled"].append(True))
        return events

    def test_transcribe_worker_declares_unified_guard_injection_seam(self):
        guard = object()
        service = object()
        worker = TranscribeWorker(
            self.source_path,
            "local_sensevoice",
            True,
            local_file_transcription_authorization_guard=guard,
            local_file_transcription_service=service,
        )
        self.assertIs(worker._local_file_transcription_authorization_guard, guard)
        self.assertIs(worker._local_file_transcription_service, service)
        parameters = inspect.signature(TranscribeWorker.__init__).parameters
        self.assertNotIn("local_file_transcription_authorization_policy", parameters)

    def test_non_none_guard_routes_to_injected_service_and_matches_direct_result(self):
        contracts, _application, guard, service = self._service()
        direct_progress = []
        direct_result = service.transcribe(
            contracts.LocalFileTranscriptionRequestV1(self.source_path),
            progress=lambda *event: direct_progress.append(event),
            is_cancelled=lambda: False,
        )
        worker = self._worker(guard, service)
        worker_events = self._capture(worker)
        legacy = Mock(side_effect=AssertionError("legacy pipeline route used"))
        with patch("src.ui.worker.transcribe_source", legacy):
            worker.run()

        self.assertEqual(len(worker_events["finished_ok"]), 1)
        self.assertEqual(worker_events["failed"], [])
        self.assertEqual(worker_events["cancelled"], [])
        self.assertEqual(
            _transcript_fingerprint(worker_events["finished_ok"][0]),
            _transcript_fingerprint(direct_result.to_transcript()),
        )
        self.assertEqual(
            contracts.normalize_local_transcription_progress(direct_progress),
            EXPECTED_PROGRESS,
        )
        self.assertEqual(
            contracts.normalize_local_transcription_progress(worker_events["progress"]),
            EXPECTED_PROGRESS,
        )
        self.assertEqual(len(_deterministic_pipeline_runner.calls), 2)
        self.assertEqual(len(_always_ready_model_probe.calls), 2)
        legacy.assert_not_called()

    def test_revoked_but_present_guard_never_falls_back_or_touches_source(self):
        contracts, application, guard, service = self._service()
        guard.revoke()
        worker = self._worker(
            guard,
            service,
            source_path="/definitely/missing/revoked-phase1a.wav",
        )
        events = self._capture(worker)
        legacy = Mock(side_effect=AssertionError("revoked guard used legacy route"))
        with patch("src.ui.worker.transcribe_source", legacy), patch(
            "pathlib.Path.stat", side_effect=AssertionError("stat before authorization")
        ), patch(
            "pathlib.Path.lstat", side_effect=AssertionError("lstat before authorization")
        ), patch(
            "os.stat", side_effect=AssertionError("os.stat before authorization")
        ), patch(
            "os.lstat", side_effect=AssertionError("os.lstat before authorization")
        ):
            worker.run()

        expected_failure = None
        try:
            service.transcribe(
                contracts.LocalFileTranscriptionRequestV1(
                    "/definitely/missing/revoked-phase1a.wav"
                )
            )
        except application.LocalFileTranscriptionError as exc:
            expected_failure = exc.failure
        self.assertIsNotNone(expected_failure)
        self.assertEqual(expected_failure.code, "AUTH_REQUIRED")
        self.assertEqual(events["failed"], [expected_failure.user_message])
        self.assertEqual(events["finished_ok"], [])
        self.assertEqual(events["cancelled"], [])
        legacy.assert_not_called()

    def test_pre_pipeline_cancellation_emits_only_cancelled_without_progress(self):
        _contracts, _application, guard, service = self._service()
        worker = self._worker(guard, service)
        events = self._capture(worker)
        worker.request_cancel()
        with patch(
            "src.ui.worker.transcribe_source",
            side_effect=AssertionError("legacy pipeline route used"),
        ) as legacy:
            worker.run()
        self.assertEqual(events["cancelled"], [True])
        self.assertEqual(events["finished_ok"], [])
        self.assertEqual(events["failed"], [])
        self.assertEqual(events["progress"], [])
        self.assertEqual(_deterministic_pipeline_runner.calls, [])
        legacy.assert_not_called()

    def test_cooperative_inference_cancellation_emits_only_cancelled_after_three_events(self):
        contracts, _application, guard, service = self._service()
        worker = self._worker(guard, service)
        events = self._capture(worker)

        def cancel_after_third(*_event) -> None:
            if len(events["progress"]) == 3:
                worker.request_cancel()

        worker.progress.connect(cancel_after_third)
        with patch(
            "src.ui.worker.transcribe_source",
            side_effect=AssertionError("legacy pipeline route used"),
        ) as legacy:
            worker.run()
        self.assertEqual(events["cancelled"], [True])
        self.assertEqual(events["finished_ok"], [])
        self.assertEqual(events["failed"], [])
        self.assertEqual(
            contracts.normalize_local_transcription_progress(events["progress"]),
            EXPECTED_PROGRESS[:3],
        )
        legacy.assert_not_called()

    def test_cancellation_after_service_return_cannot_revoke_finished_result(self):
        contracts, application, guard, _service = self._service()
        holder = {}

        class LateCancellationService(application.LocalFileTranscriptionApplicationService):
            def transcribe(self, request, *, progress=None, is_cancelled=None):
                result = super().transcribe(
                    request,
                    progress=progress,
                    is_cancelled=is_cancelled,
                )
                holder["worker"].request_cancel()
                return result

        service = LateCancellationService(
            guard,
            pipeline_runner=_deterministic_pipeline_runner,
            model_readiness_probe=_always_ready_model_probe,
        )
        worker = self._worker(guard, service)
        holder["worker"] = worker
        events = self._capture(worker)
        with patch(
            "src.ui.worker.transcribe_source",
            side_effect=AssertionError("legacy pipeline route used"),
        ) as legacy:
            worker.run()
        self.assertEqual(len(events["finished_ok"]), 1)
        self.assertEqual(events["cancelled"], [])
        self.assertEqual(events["failed"], [])
        self.assertEqual(
            _transcript_fingerprint(events["finished_ok"][0]),
            (
                "测试音频。",
                (("测试", 0.0, 1.0), ("音频。", 1.0, 3.0)),
                "zh",
                "local_sensevoice",
                self.source_path,
            ),
        )
        self.assertEqual(
            contracts.normalize_local_transcription_progress(events["progress"]),
            EXPECTED_PROGRESS,
        )
        legacy.assert_not_called()

    def test_missing_source_model_not_ready_and_engine_failure_match_direct_user_message(self):
        contracts, application, guard, _service = self._service()

        def not_ready(_engine_name):
            return False

        def failing_runner(*_args, **_kwargs):
            raise RuntimeError("private engine detail")

        cases = [
            (str(Path(self._tmp.name) / "missing.wav"), _always_ready_model_probe, _deterministic_pipeline_runner),
            (self.source_path, not_ready, _deterministic_pipeline_runner),
            (self.source_path, _always_ready_model_probe, failing_runner),
        ]
        for source_path, readiness, runner in cases:
            with self.subTest(source_path=source_path, readiness=readiness.__name__):
                service = application.LocalFileTranscriptionApplicationService(
                    guard,
                    pipeline_runner=runner,
                    model_readiness_probe=readiness,
                )
                request = contracts.LocalFileTranscriptionRequestV1(source_path)
                with self.assertRaises(application.LocalFileTranscriptionError) as direct:
                    service.transcribe(request)
                worker = self._worker(guard, service, source_path=source_path)
                events = self._capture(worker)
                with patch(
                    "src.ui.worker.transcribe_source",
                    side_effect=AssertionError("legacy pipeline route used"),
                ) as legacy:
                    worker.run()
                self.assertEqual(events["failed"], [direct.exception.failure.user_message])
                self.assertEqual(events["finished_ok"], [])
                self.assertEqual(events["cancelled"], [])
                legacy.assert_not_called()

    def test_guard_none_and_excluded_routes_preserve_legacy_path(self):
        transcript = Transcript(text="legacy")
        legacy = Mock(return_value=transcript)
        excluded = [
            (None, object(), self.source_path, "local_sensevoice", None),
            (object(), object(), "https://example.invalid/audio", "local_sensevoice", None),
            (object(), object(), self.source_path, "cloud_asr", None),
        ]
        for guard, service, source, engine, execution_policy in excluded:
            with self.subTest(source=source, engine=engine, guard=guard):
                worker = TranscribeWorker(
                    source,
                    engine,
                    True,
                    execution_policy=execution_policy,
                    local_file_transcription_authorization_guard=guard,
                    local_file_transcription_service=service,
                )
                events = self._capture(worker)
                with patch("src.ui.worker.transcribe_source", legacy):
                    worker.run()
                self.assertEqual(events["finished_ok"], [transcript])
                self.assertEqual(events["failed"], [])
                self.assertEqual(events["cancelled"], [])
        self.assertEqual(legacy.call_count, len(excluded))


if __name__ == "__main__":
    unittest.main()
