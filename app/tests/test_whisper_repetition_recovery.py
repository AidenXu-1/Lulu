"""Real-batch regressions: extreme repetition and speech beyond audio duration.

No model, account, network or user storage is used here. The live two-video
batch separately verifies the installed local model and visible GUI.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from src.core.engines.local_native import IncompleteTranscription
from src.core.engines.local_whisper_mlx import LocalWhisperMLXEngine
from src.core.engines import asr_resource
from tests import test_mlx_whisper_progress as bridge_tests
from src.core.engines.mlx_whisper_progress import transcribe_with_audio_progress


def clean():
    return {"text": "正常正文，欢迎留言。", "language": "zh", "segments": [
        {"text": "正常正文，欢迎留言。", "start": 0., "end": 2.}]}


def bad():
    return {"text": "正常正文。" + "下面" * 221, "language": "zh", "segments": [
        {"text": "正常正文。", "start": 0., "end": 100.72},
        {"text": "下面" * 221, "start": 100.72, "end": 100.93}]}


class WhisperRepetitionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch("src.core.models_manager.app_data_dir", return_value=self.root))
        self.stack.enter_context(patch("src.core.models_manager.model_path", return_value=self.root / "model"))
        self.stack.enter_context(patch("src.core.models_manager.is_downloaded", return_value=True))
        self.stack.enter_context(patch("soundfile.read", return_value=(np.zeros(120 * 16000, dtype=np.float32), 16000)))
        self.infer = self.stack.enter_context(patch("src.core.engines.local_whisper_mlx.transcribe_with_audio_progress"))
        self.engine = LocalWhisperMLXEngine("local_whisper_mlx_4bit")

    def test_clean_result_keeps_original_single_call_and_mapping(self):
        self.infer.return_value = clean()
        result = self.engine.transcribe(Path("fixture.wav"))
        self.assertEqual(result.text, clean()["text"])
        self.assertEqual(result.segments[0].end, 2.)
        self.infer.assert_called_once()
        self.assertNotIn("temperature", self.infer.call_args.kwargs)
        self.assertNotIn("word_timestamps", self.infer.call_args.kwargs)

    def test_impossible_repetition_retries_same_audio_once_with_supported_options(self):
        self.infer.side_effect = [bad(), clean()]
        result = self.engine.transcribe(Path("fixture.wav"))
        self.assertEqual(result.text, clean()["text"])
        self.assertEqual(self.infer.call_count, 2)
        first, retry = self.infer.call_args_list
        self.assertIs(first.args[0], retry.args[0])
        self.assertIs(first.kwargs["is_cancelled"], retry.kwargs["is_cancelled"])
        self.assertIs(retry.kwargs["condition_on_previous_text"], False)
        self.assertEqual(retry.kwargs["temperature"], 0.)
        self.assertEqual(len(list((self.root / "incomplete-transcriptions").glob("*.json"))), 1)

    def test_repeated_retry_is_incomplete_with_private_evidence_and_released_resource(self):
        self.infer.side_effect = [bad(), bad()]
        with self.assertRaises(IncompleteTranscription) as error:
            self.engine.transcribe(Path("fixture.wav"))
        self.assertEqual(self.infer.call_count, 2)
        files = list((self.root / "incomplete-transcriptions").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        stored = json.loads(files[0].read_text())
        self.assertEqual([x["text"] for x in stored["attempts"]], [bad()["text"], bad()["text"]])
        self.assertIn(str(files[0]), str(error.exception))
        with asr_resource.maintenance():
            pass

    def test_cancel_between_attempts_never_starts_retry(self):
        self.infer.return_value = bad()
        cancelled = threading.Event()
        def progress(message, _fraction):
            if "异常" in message:
                cancelled.set()
        with self.assertRaises(InterruptedError):
            self.engine.transcribe(Path("fixture.wav"), progress, cancelled.is_set)
        self.infer.assert_called_once()
        self.assertEqual(len(list((self.root / "incomplete-transcriptions").glob("*.json"))), 1)

    def test_failed_evidence_save_stops_without_retry_or_success(self):
        self.infer.return_value = bad()
        with patch("src.core.updater.secure_io.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(IncompleteTranscription) as error:
                self.engine.transcribe(Path("fixture.wav"))
        self.infer.assert_called_once()
        self.assertIn("未能保存", str(error.exception))
        with asr_resource.maintenance():
            pass

    def test_retry_runtime_failure_keeps_first_evidence_and_never_calls_again(self):
        self.infer.side_effect = [bad(), RuntimeError("inference failed")]
        with self.assertRaisesRegex(RuntimeError, "inference failed"):
            self.engine.transcribe(Path("fixture.wav"))
        self.assertEqual(self.infer.call_count, 2)
        files = list((self.root / "incomplete-transcriptions").glob("*.json"))
        self.assertEqual(json.loads(files[0].read_text())["attempts"][0]["text"], bad()["text"])
        with asr_resource.maintenance():
            pass

    def test_legitimate_slow_repetition_is_preserved_without_retry(self):
        value = {"text": "练习" * 100, "language": "zh", "segments": [
            {"text": "练习" * 100, "start": 0., "end": 100.}]}
        self.infer.return_value = value
        self.assertEqual(self.engine.transcribe(Path("fixture.wav")).text, value["text"])
        self.infer.assert_called_once()

    def test_short_stutter_is_preserved(self):
        self.infer.return_value = {"text": "我我我", "language": "zh", "segments": [
            {"text": "我我我", "start": 0., "end": .02}]}
        self.assertEqual(self.engine.transcribe(Path("fixture.wav")).text, "我我我")
        self.infer.assert_called_once()

    def test_hallucinated_tail_beyond_actual_audio_retries_without_trimming(self):
        value = {"text": "正常正文。CircleCircle", "segments": [
            {"text": "正常正文。", "start": 0., "end": 119.5},
            {"text": "Circle", "start": 119.5, "end": 122.78},
            {"text": "Circle", "start": 122.78, "end": 125.32}]}
        self.infer.side_effect = [value, clean()]
        self.assertEqual(self.engine.transcribe(Path("fixture.wav")).text, clean()["text"])
        self.assertEqual(self.infer.call_count, 2)
        self.assertIs(self.infer.call_args.kwargs["word_timestamps"], True)
        evidence = next((self.root / "incomplete-transcriptions").glob("*.json"))
        stored = json.loads(evidence.read_text())
        self.assertEqual(stored["reason"], "invalid_audio_timing")
        self.assertEqual(stored["attempts"][0]["text"], value["text"])

    def test_small_timestamp_rounding_at_audio_end_is_preserved(self):
        value = {"text": "正常尾句。", "segments": [
            {"text": "正常尾句。", "start": 119., "end": 120.1}]}
        self.infer.return_value = value
        self.assertEqual(self.engine.transcribe(Path("fixture.wav")).text, value["text"])
        self.infer.assert_called_once()

    def test_repetition_across_segment_boundaries_also_retries(self):
        value = {"text": "下面" * 96, "language": "zh", "segments": [
            {"text": "下面" * 12, "start": i * .03, "end": (i + 1) * .03} for i in range(8)]}
        self.infer.side_effect = [value, clean()]
        self.assertEqual(self.engine.transcribe(Path("fixture.wav")).text, clean()["text"])
        self.assertEqual(self.infer.call_count, 2)


class WhisperRecoveryBridgeTests(unittest.TestCase):
    def test_recovery_options_reach_upstream_and_restore_cancellation_bridge(self):
        package, module = bridge_tests.WhisperBridgeTests._fake_modules()
        options = []
        def upstream(audio, **kwargs):
            options.append(kwargs)
            model = module.ModelHolder.get_model(kwargs["path_or_hf_repo"], None)
            with module.tqdm.tqdm(total=3000) as bar:
                model.decode(object(), None)
                bar.update(3000)
            return clean()
        package.transcribe = upstream
        original = module.ModelHolder.__dict__["get_model"]
        with patch.dict("sys.modules", {"mlx_whisper": package, "mlx_whisper.transcribe": module}):
            result = transcribe_with_audio_progress(None, path_or_hf_repo="fixture", on_audio_position=lambda _:None,
                is_cancelled=lambda:False, condition_on_previous_text=False, temperature=0., word_timestamps=True)
        self.assertEqual(result, clean())
        self.assertEqual(options, [{"path_or_hf_repo": "fixture", "condition_on_previous_text": False, "temperature": 0., "word_timestamps": True}])
        self.assertIs(module.ModelHolder.__dict__["get_model"], original)
