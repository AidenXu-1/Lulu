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


if __name__ == "__main__":
    unittest.main()
