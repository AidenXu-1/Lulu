"""Recorder callback ordering without opening a real microphone or permission UI."""
from unittest import TestCase
from unittest.mock import MagicMock, patch
from src.ui import recorder


class MicrophoneRecorderResultTests(TestCase):
    def setUp(self):
        self.native = MagicMock()
        original = recorder.QMediaRecorder
        for name in ('QMediaCaptureSession', 'QAudioInput', 'QMediaRecorder'):
            replacement = patch.object(recorder, name)
            mocked = replacement.start(); self.addCleanup(replacement.stop)
            if name == 'QMediaRecorder':
                mocked.return_value = self.native
                mocked.RecorderState = original.RecorderState
                mocked.Quality = original.Quality
                mocked.Error = original.Error
        self.stopped = original.RecorderState.StoppedState
        self.error = original.Error.ResourceError
        self.subject = recorder.MicRecorder()
        self.subject.start('/tmp/qa-only-microphone.m4a')
        self.finished = []; self.failed = []
        self.subject.finished.connect(self.finished.append)
        self.subject.failed.connect(self.failed.append)
        self.native.actualLocation.return_value.toLocalFile.return_value = '/tmp/qa-only-microphone.m4a'

    def test_zero_duration_does_not_publish_container_only_recording(self):
        self.native.duration.return_value = 0
        self.subject._on_state(self.stopped)
        self.assertEqual(self.finished, [])
        self.assertEqual(len(self.failed), 1)

    def test_error_followed_by_stopped_never_publishes_success(self):
        self.native.duration.return_value = 1000
        self.subject._on_error(self.error, 'device unavailable')
        self.subject._on_state(self.stopped)
        self.assertEqual(self.finished, [])
        self.assertEqual(self.failed, ['device unavailable'])
        self.subject.start('/tmp/qa-only-microphone-retry.m4a')
        self.subject._on_state(self.stopped)
        self.assertEqual(len(self.finished), 1)

    def test_valid_recording_finishes_once_and_next_attempt_recovers(self):
        self.native.duration.return_value = 1000
        self.subject._on_state(self.stopped)
        self.subject._on_state(self.stopped)
        self.assertEqual(len(self.finished), 1)
        self.subject.start('/tmp/qa-only-microphone-next.m4a')
        self.subject._on_state(self.stopped)
        self.assertEqual(len(self.finished), 2)
