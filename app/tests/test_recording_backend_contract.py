from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.recording import (
    RecordingBackend,
    RecordingRequest,
    RecordingResult,
    RecordingSession,
    RecordingSource,
    RecordingTrack,
    RecordingTrackKind,
    RecordingUse,
)


class _Session:
    def __init__(self, request: RecordingRequest):
        self._request = request
        self._active = True

    @property
    def request(self) -> RecordingRequest:
        return self._request

    def stop(self, progress=None) -> RecordingResult:
        self._active = False
        track = RecordingTrack(
            kind=RecordingTrackKind.SYSTEM_AUDIO,
            path=self._request.output_path,
            duration_seconds=6.0,
            sample_rate=48000,
            channels=2,
            is_silent=False,
        )
        return RecordingResult(
            source=self._request.source,
            output_path=self._request.output_path,
            tracks=(track,),
            duration_seconds=6.0,
            sample_rate=48000,
            channels=2,
        )

    def cancel(self) -> None:
        self._active = False

    def is_active(self) -> bool:
        return self._active


class _Backend:
    backend_id = "macos_screencapturekit_spike"
    display_name = "macOS ScreenCaptureKit Spike"
    platforms = ("darwin",)

    def supports(self, source: RecordingSource) -> bool:
        return source in {RecordingSource.SYSTEM_AUDIO, RecordingSource.MIXED}

    def start(self, request: RecordingRequest, progress=None) -> RecordingSession:
        if not self.supports(request.source):
            raise ValueError("unsupported recording source")
        return _Session(request)


class RecordingBackendContractTests(unittest.TestCase):
    def test_sources_are_platform_neutral_product_choices(self):
        self.assertEqual(RecordingSource.MICROPHONE.value, "microphone")
        self.assertEqual(RecordingSource.SYSTEM_AUDIO.value, "system_audio")
        self.assertEqual(RecordingSource.MIXED.value, "mixed")

    def test_request_keeps_purpose_separate_from_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = RecordingRequest(
                source=RecordingSource.SYSTEM_AUDIO,
                output_path=Path(tmp) / "system.m4a",
                use=RecordingUse.EMOTION_REFERENCE,
                max_duration_seconds=8,
            )

        self.assertEqual(request.source, RecordingSource.SYSTEM_AUDIO)
        self.assertEqual(request.use, RecordingUse.EMOTION_REFERENCE)
        self.assertEqual(request.max_duration_seconds, 8)

    def test_backend_protocol_hides_platform_specific_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend: RecordingBackend = _Backend()
            request = RecordingRequest(
                source=RecordingSource.SYSTEM_AUDIO,
                output_path=Path(tmp) / "system.m4a",
            )
            session = backend.start(request)
            result = session.stop()

        self.assertFalse(session.is_active())
        self.assertEqual(result.source, RecordingSource.SYSTEM_AUDIO)
        self.assertEqual(result.output_path.name, "system.m4a")
        self.assertTrue(result.has_audio)
        self.assertEqual(result.tracks[0].kind, RecordingTrackKind.SYSTEM_AUDIO)

    def test_silent_tracks_do_not_count_as_audio(self):
        result = RecordingResult(
            source=RecordingSource.SYSTEM_AUDIO,
            output_path=Path("silent.m4a"),
            tracks=(
                RecordingTrack(
                    kind=RecordingTrackKind.SYSTEM_AUDIO,
                    path=Path("silent.m4a"),
                    is_silent=True,
                ),
            ),
        )

        self.assertFalse(result.has_audio)


if __name__ == "__main__":
    unittest.main()
