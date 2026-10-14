from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Literal
import unittest
from unittest.mock import patch

from src.core.models import Segment, Transcript
from src.core.pipeline import _Cancelled


FIXTURE_NAME = "phase1a_local_sensevoice_v1.wav"
FIXTURE_SIZE = 96044
FIXTURE_SHA256 = "884c1d21de9b663c293fcd09a3125728a2e63cac635a8576aa63e5a9e68e0f92"
EXPECTED_PROGRESS = [
    ("准备输入…", 0.05),
    ("转码音频(16k 单声道)…", 0.22),
    ("使用引擎:本地 · SenseVoice-Small(离线)", 0.32),
    ("识别中…", 0.8),
    ("正在整理并保存转录结果…", 0.97),
    ("完成", 1.0),
]


def _load_phase1a_modules():
    contracts = importlib.import_module("src.core.transcription_contracts")
    application = importlib.import_module("src.core.transcription_application")
    return contracts, application


def _phase1a_wav_bytes() -> bytes:
    frame_count = 48000
    sample_rate = 16000
    data = b"".join(
        struct.pack("<h", 12000 if (index // 40) % 2 == 0 else -12000)
        for index in range(frame_count)
    )
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(data),
    )
    return header + data


def _write_phase1a_fixture(directory: str | Path) -> str:
    payload = _phase1a_wav_bytes()
    if len(payload) != FIXTURE_SIZE:
        raise AssertionError(f"fixture size drifted: {len(payload)}")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != FIXTURE_SHA256:
        raise AssertionError(f"fixture digest drifted: {digest}")
    path = Path(directory) / FIXTURE_NAME
    path.write_bytes(payload)
    return str(path)


def _deterministic_pipeline_runner(
    source_path: str,
    *,
    engine_name: Literal["local_sensevoice"],
    progress,
    is_cancelled,
) -> Transcript:
    _deterministic_pipeline_runner.calls.append((source_path, engine_name))
    if engine_name != "local_sensevoice":
        raise AssertionError(f"unexpected engine: {engine_name}")
    payload = Path(source_path).read_bytes()
    if len(payload) != FIXTURE_SIZE:
        raise AssertionError(f"unexpected fixture size: {len(payload)}")
    if hashlib.sha256(payload).hexdigest() != FIXTURE_SHA256:
        raise AssertionError("unexpected fixture digest")
    for index, event in enumerate(EXPECTED_PROGRESS):
        if progress is not None:
            progress(*event)
        if index == 2 and is_cancelled():
            raise _Cancelled()
    return Transcript(
        text="测试音频。",
        segments=[
            Segment(text="测试", start=0.0, end=1.0),
            Segment(text="音频。", start=1.0, end=3.0),
        ],
        language="zh",
        engine="local_sensevoice",
        source=source_path,
        title=None,
        meta={},
        saved_media=None,
    )


_deterministic_pipeline_runner.calls = []


def _always_ready_model_probe(
    engine_name: Literal["local_sensevoice"],
) -> bool:
    _always_ready_model_probe.calls.append(engine_name)
    if engine_name != "local_sensevoice":
        raise AssertionError(f"unexpected engine: {engine_name}")
    return True


_always_ready_model_probe.calls = []


def _reset_phase1a_stub_calls() -> None:
    _deterministic_pipeline_runner.calls.clear()
    _always_ready_model_probe.calls.clear()


class _ProjectionServiceStub:
    def __init__(self, *, allowed: bool = True, code: str = "cached_allowed") -> None:
        self.status = SimpleNamespace(allowed=allowed, code=code)
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return self.status


def _unified_guard(*, allowed: bool = True, code: str = "cached_allowed"):
    from src.core.auth_guard import UnifiedLicenseAuthGuard

    return UnifiedLicenseAuthGuard(
        _ProjectionServiceStub(allowed=allowed, code=code)
    )


def _transcript_fingerprint(transcript: Transcript) -> tuple:
    return (
        transcript.text,
        tuple((segment.text, segment.start, segment.end) for segment in transcript.segments),
        transcript.language,
        transcript.engine,
        transcript.source,
    )


class _SequencedCancellationProbe:
    def __init__(self, *answers: bool):
        self._answers = iter(answers)
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return next(self._answers, False)


class Phase1AApplicationServiceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lulu-phase1a-core-")
        self.addCleanup(self._tmp.cleanup)
        self.source_path = _write_phase1a_fixture(self._tmp.name)
        _reset_phase1a_stub_calls()

    def _guard_and_service(self, *, runner=None, readiness=None):
        _contracts, application = _load_phase1a_modules()
        guard = _unified_guard()
        service = application.LocalFileTranscriptionApplicationService(
            guard,
            pipeline_runner=runner or _deterministic_pipeline_runner,
            model_readiness_probe=readiness or _always_ready_model_probe,
        )
        return application, guard, service

    def _request(self, source_path: str | None = None, engine_name: str = "sensevoice"):
        contracts, _application = _load_phase1a_modules()
        return contracts.LocalFileTranscriptionRequestV1(
            source_path=self.source_path if source_path is None else source_path,
            engine_name=engine_name,
        )

    def assertFailureCode(self, application, expected_code: str, callable_):
        with self.assertRaises(application.LocalFileTranscriptionError) as caught:
            callable_()
        self.assertEqual(caught.exception.failure.code, expected_code)
        self.assertIsInstance(caught.exception.failure.user_message, str)
        self.assertTrue(caught.exception.failure.user_message)
        return caught.exception.failure

    def test_active_unified_guard_transcribes_fixture_without_external_effects(self):
        contracts, _application = _load_phase1a_modules()
        application, _guard, service = self._guard_and_service()
        progress = []

        result = service.transcribe(
            self._request(),
            progress=lambda message, fraction: progress.append((message, fraction)),
            is_cancelled=lambda: False,
        )

        self.assertIsInstance(result, contracts.LocalFileTranscriptionResultV1)
        self.assertEqual(result.text, "测试音频。")
        self.assertEqual(
            tuple((segment.text, segment.start, segment.end) for segment in result.segments),
            (("测试", 0.0, 1.0), ("音频。", 1.0, 3.0)),
        )
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.engine, "local_sensevoice")
        self.assertEqual(result.source_path, self.source_path)
        self.assertEqual(
            contracts.normalize_local_transcription_progress(progress),
            EXPECTED_PROGRESS,
        )
        self.assertEqual(
            _deterministic_pipeline_runner.calls,
            [(self.source_path, "local_sensevoice")],
        )
        self.assertEqual(_always_ready_model_probe.calls, ["local_sensevoice"])
        self.assertIs(
            service.__class__,
            application.LocalFileTranscriptionApplicationService,
        )

    def test_unified_guard_precedes_source_access_and_maps_secure_store_failure(self):
        from src.core.auth_guard import UnifiedLicenseAuthGuard

        contracts, application = _load_phase1a_modules()

        active_projection = _ProjectionServiceStub(allowed=True, code="cached_allowed")
        active_service = application.LocalFileTranscriptionApplicationService(
            UnifiedLicenseAuthGuard(active_projection),
            pipeline_runner=_deterministic_pipeline_runner,
            model_readiness_probe=_always_ready_model_probe,
        )
        result = active_service.transcribe(self._request())
        self.assertEqual(result.engine, "local_sensevoice")
        self.assertEqual(active_projection.calls, 1)

        _reset_phase1a_stub_calls()
        locked_projection = _ProjectionServiceStub(
            allowed=False,
            code="secure_store_error",
        )
        locked_service = application.LocalFileTranscriptionApplicationService(
            UnifiedLicenseAuthGuard(locked_projection),
            pipeline_runner=_deterministic_pipeline_runner,
            model_readiness_probe=_always_ready_model_probe,
        )
        missing_request = contracts.LocalFileTranscriptionRequestV1(
            "/definitely/missing/phase2.wav"
        )
        with patch(
            "os.lstat",
            side_effect=AssertionError("source touched before unified guard"),
        ), patch(
            "builtins.open",
            side_effect=AssertionError("source opened before unified guard"),
        ):
            self.assertFailureCode(
                application,
                "SECURE_STORE_UNAVAILABLE",
                lambda: locked_service.transcribe(missing_request),
            )
        self.assertEqual(locked_projection.calls, 1)
        self.assertEqual(_deterministic_pipeline_runner.calls, [])
        self.assertEqual(_always_ready_model_probe.calls, [])

    def test_dtos_are_frozen_closed_and_map_exactly_to_existing_transcript(self):
        contracts, _application = _load_phase1a_modules()
        expected_fields = {
            contracts.LocalFileTranscriptionRequestV1: ("source_path", "engine_name"),
            contracts.LocalTranscriptionSegmentV1: ("text", "start", "end"),
            contracts.LocalFileTranscriptionResultV1: (
                "text",
                "segments",
                "language",
                "engine",
                "source_path",
            ),
            contracts.LocalFileTranscriptionFailureV1: ("code", "user_message"),
        }
        for dto_type, field_names in expected_fields.items():
            with self.subTest(dto=dto_type.__name__):
                self.assertTrue(dataclasses.is_dataclass(dto_type))
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(dto_type)),
                    field_names,
                )

        request = self._request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.source_path = "mutated"  # type: ignore[misc]

        result = contracts.LocalFileTranscriptionResultV1(
            text="测试音频。",
            segments=(
                contracts.LocalTranscriptionSegmentV1("测试", 0.0, 1.0),
                contracts.LocalTranscriptionSegmentV1("音频。", 1.0, 3.0),
            ),
            language="zh",
            engine="local_sensevoice",
            source_path=self.source_path,
        )
        transcript = result.to_transcript()
        self.assertEqual(
            _transcript_fingerprint(transcript),
            (
                "测试音频。",
                (("测试", 0.0, 1.0), ("音频。", 1.0, 3.0)),
                "zh",
                "local_sensevoice",
                self.source_path,
            ),
        )
        self.assertIsNone(transcript.title)
        self.assertEqual(transcript.meta, {})
        self.assertIsNone(transcript.saved_media)

    def test_core_imports_do_not_load_qt(self):
        script = (
            "import sys; "
            "import src.core.transcription_contracts; "
            "import src.core.transcription_application; "
            "print(','.join(sorted(name for name in sys.modules if name.startswith('PySide6'))))"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path.cwd() / "app")
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "\n")

    def test_progress_normalizer_preserves_bytes_clamps_and_only_deduplicates_neighbors(self):
        contracts, _application = _load_phase1a_modules()
        raw = [
            ("原样…", -9.0),
            ("原样…", -1.0),
            ("同一文案", 0.12345649),
            ("同一文案", 0.12345641),
            ("上限", 8.0),
            ("原样…", -1.0),
        ]
        self.assertEqual(
            contracts.normalize_local_transcription_progress(raw),
            [
                ("原样…", -1.0),
                ("同一文案", 0.123456),
                ("上限", 1.0),
                ("原样…", -1.0),
            ],
        )
        with self.assertRaises((TypeError, ValueError)):
            contracts.normalize_local_transcription_progress([("", 0.5)])

    def test_only_exact_unified_guard_is_revocable_and_precedes_source_effects(self):
        contracts, application = _load_phase1a_modules()
        parameters = inspect.signature(
            application.LocalFileTranscriptionApplicationService.__init__
        ).parameters
        self.assertIn("authorization_guard", parameters)
        self.assertNotIn("authorization_policy", parameters)

        class ForeignPolicy:
            calls = 0

            def assert_local_file_transcription_allowed(self, _request) -> None:
                self.calls += 1

        active = _unified_guard()
        revoked = _unified_guard()
        revoked.revoke()
        request = contracts.LocalFileTranscriptionRequestV1(
            source_path="/definitely/missing/phase1a.wav"
        )

        for policy in (None, revoked, ForeignPolicy(), object()):
            _reset_phase1a_stub_calls()
            service = application.LocalFileTranscriptionApplicationService(
                policy,
                pipeline_runner=_deterministic_pipeline_runner,
                model_readiness_probe=_always_ready_model_probe,
            )
            with self.subTest(policy=type(policy).__name__), patch(
                "pathlib.Path.stat", side_effect=AssertionError("stat before policy")
            ), patch(
                "pathlib.Path.lstat", side_effect=AssertionError("lstat before policy")
            ), patch(
                "os.stat", side_effect=AssertionError("os.stat before policy")
            ), patch(
                "os.lstat", side_effect=AssertionError("os.lstat before policy")
            ), patch(
                "builtins.open", side_effect=AssertionError("open before policy")
            ), patch(
                "os.open", side_effect=AssertionError("os.open before policy")
            ):
                self.assertFailureCode(
                    application,
                    "AUTH_REQUIRED",
                    lambda: service.transcribe(request),
                )
            self.assertEqual(_deterministic_pipeline_runner.calls, [])
            self.assertEqual(_always_ready_model_probe.calls, [])

        active_service = application.LocalFileTranscriptionApplicationService(
            active,
            pipeline_runner=_deterministic_pipeline_runner,
            model_readiness_probe=_always_ready_model_probe,
        )
        result = active_service.transcribe(self._request())
        self.assertEqual(result.engine, "local_sensevoice")
        self.assertFalse(hasattr(application, "Phase1AFormalGuiMarkerPolicy"))
        self.assertFalse(
            hasattr(application, "issue_phase1a_formal_gui_marker_policy")
        )

    def test_closed_request_and_regular_no_symlink_path_validation(self):
        application, _guard, service = self._guard_and_service()
        cases = [
            ("https://example.invalid/audio.wav", "INVALID_ARGUMENT"),
            (str(Path(self._tmp.name) / "missing.wav"), "NOT_FOUND"),
            (self._tmp.name, "UNSAFE_PATH"),
        ]
        symlink = Path(self._tmp.name) / "linked.wav"
        symlink.symlink_to(self.source_path)
        cases.append((str(symlink), "UNSAFE_PATH"))
        for source_path, code in cases:
            with self.subTest(source_path=source_path):
                self.assertFailureCode(
                    application,
                    code,
                    lambda p=source_path: service.transcribe(self._request(p)),
                )

        self.assertFailureCode(
            application,
            "INVALID_ARGUMENT",
            lambda: service.transcribe(self._request(engine_name="cloud")),
        )
        self.assertEqual(_deterministic_pipeline_runner.calls, [])
        self.assertEqual(_always_ready_model_probe.calls, [])

    def test_model_readiness_precedes_runner_and_does_not_fallback(self):
        runner_calls = []
        readiness_calls = []

        def runner(*_args, **_kwargs):
            runner_calls.append("called")
            raise AssertionError("runner must not run")

        def not_ready(engine_name):
            readiness_calls.append(engine_name)
            return False

        application, _guard, service = self._guard_and_service(
            runner=runner,
            readiness=not_ready,
        )
        self.assertFailureCode(
            application,
            "MODEL_NOT_READY",
            lambda: service.transcribe(self._request()),
        )
        self.assertEqual(readiness_calls, ["local_sensevoice"])
        self.assertEqual(runner_calls, [])

    def test_unexpected_runner_failure_is_sanitized_internal_error(self):
        def failing_runner(*_args, **_kwargs):
            raise RuntimeError("secret-token=/Users/private/model.bin")

        application, _guard, service = self._guard_and_service(
            runner=failing_runner,
        )
        failure = self.assertFailureCode(
            application,
            "INTERNAL_ERROR",
            lambda: service.transcribe(self._request()),
        )
        self.assertNotIn("secret-token", failure.user_message)
        self.assertNotIn("/Users/private", failure.user_message)

    def test_cancellation_before_model_has_no_progress_or_pipeline_call(self):
        application, _guard, service = self._guard_and_service()
        progress = []
        self.assertFailureCode(
            application,
            "CANCELLED",
            lambda: service.transcribe(
                self._request(),
                progress=lambda *event: progress.append(event),
                is_cancelled=lambda: True,
            ),
        )
        self.assertEqual(progress, [])
        self.assertEqual(_always_ready_model_probe.calls, [])
        self.assertEqual(_deterministic_pipeline_runner.calls, [])

    def test_cooperative_inference_cancellation_preserves_first_three_progress_events(self):
        contracts, _application = _load_phase1a_modules()
        application, _guard, service = self._guard_and_service()
        progress = []
        cancellation = _SequencedCancellationProbe(False, True)
        self.assertFailureCode(
            application,
            "CANCELLED",
            lambda: service.transcribe(
                self._request(),
                progress=lambda *event: progress.append(event),
                is_cancelled=cancellation,
            ),
        )
        self.assertEqual(
            contracts.normalize_local_transcription_progress(progress),
            EXPECTED_PROGRESS[:3],
        )
        self.assertEqual(_always_ready_model_probe.calls, ["local_sensevoice"])
        self.assertEqual(len(_deterministic_pipeline_runner.calls), 1)

    def test_post_pipeline_precommit_cancellation_still_cancels(self):
        def returning_runner(source_path, **_kwargs):
            return Transcript(
                text="测试音频。",
                segments=[],
                language="zh",
                engine="local_sensevoice",
                source=source_path,
            )

        application, _guard, service = self._guard_and_service(
            runner=returning_runner,
        )
        cancellation = _SequencedCancellationProbe(False, True)
        self.assertFailureCode(
            application,
            "CANCELLED",
            lambda: service.transcribe(
                self._request(),
                is_cancelled=cancellation,
            ),
        )
        self.assertEqual(cancellation.calls, 2)

    def test_successful_return_is_the_result_boundary(self):
        application, _guard, service = self._guard_and_service()
        state = SimpleNamespace(cancelled=False)
        result = service.transcribe(
            self._request(),
            is_cancelled=lambda: state.cancelled,
        )
        state.cancelled = True
        self.assertEqual(result.text, "测试音频。")
        self.assertEqual(result.engine, "local_sensevoice")


if __name__ == "__main__":
    unittest.main()
