"""0.2.6 素材偏好快照、任务策略与运行列隔离。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppConfig, COLLECT_COVER_CONTRACT_VERSION  # noqa: E402
from src.core.collect.preferences import (  # noqa: E402
    MaterialPreferenceService,
    MaterialPreferenceSnapshot,
)
from src.core.collect.media import ManagedMediaValidation  # noqa: E402
from src.core.task import (  # noqa: E402
    Task,
    TaskExecutionPolicy,
    TaskState,
    derive_collection_item_runtime,
)
from src.core import pipeline  # noqa: E402
from src.core.models import Transcript  # noqa: E402


class FakeConfig:
    def __init__(self):
        self.link_media_mode = "audio"
        self.keep_media = False
        self.collect_cover = False
        self.collect_cover_contract_version = 0
        self.saves = 0
        self.save_result = True

    def save(self):
        self.saves += 1
        return self.save_result


class MaterialPreferenceTests(unittest.TestCase):
    def setUp(self):
        # 本文件验证本地素材/策略；新的采集地址路径由独立测试覆盖。
        fixture = patch("src.core.collect.transcription_input.download_collected_input", return_value=None)
        fixture.start()
        self.addCleanup(fixture.stop)











    def test_batch_transcription_reuses_valid_managed_media_without_link_download(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:local",
            snapshot=MaterialPreferenceSnapshot("video", True, True),
        )

        class Engine:
            display_name = "Fake"

            def transcribe(self, path, _progress):
                self.seen = path
                return Transcript("ok")

        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw) / "managed.mp4"
            managed.write_bytes(b"managed-video")
            resolved_sources = []
            converted_outputs = []
            engine = Engine()

            def resolve(source, _tmp, *_args, **_kwargs):
                resolved_sources.append(source)
                return Path(source)

            def convert(source, output, *, is_cancelled, timeout):
                self.assertTrue(callable(is_cancelled))
                self.assertEqual(timeout, 600)
                self.assertEqual(source, managed)
                output.write_bytes(b"wav")
                converted_outputs.append(output)
                return output

            with (
                patch.object(
                    pipeline,
                    "resolve_managed_transcription_media",
                    return_value=ManagedMediaValidation(
                        True,
                        managed,
                        managed.stat().st_size,
                    ),
                ) as lookup,
                patch.object(pipeline, "resolve_source", side_effect=resolve),
                patch.object(pipeline, "to_wav_16k_mono", side_effect=convert),
                patch.object(pipeline, "get_engine", return_value=engine),
            ):
                transcript = pipeline.transcribe_source(
                    "https://example.test/work/local",
                    execution_policy=policy,
                )

            lookup.assert_called_once_with("douyin:local")
            self.assertEqual(resolved_sources, [str(managed)])
            self.assertEqual(transcript.source, "https://example.test/work/local")
            self.assertIsNone(transcript.saved_media)
            self.assertTrue(managed.exists())
            self.assertEqual(managed.read_bytes(), b"managed-video")
            self.assertEqual(len(converted_outputs), 1)
            self.assertFalse(converted_outputs[0].exists())


    def test_batch_transcription_cancelled_local_attempt_never_falls_back(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:cancel",
            snapshot=MaterialPreferenceSnapshot("audio", False, False),
        )

        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw) / "managed.m4a"
            managed.write_bytes(b"managed-audio")
            cancelled = False
            sources = []

            def resolve(source, *_args, **_kwargs):
                sources.append(source)
                return managed

            def convert(_source, _output, *, is_cancelled, timeout):
                nonlocal cancelled
                self.assertTrue(callable(is_cancelled))
                self.assertEqual(timeout, 600)
                cancelled = True
                raise InterruptedError("cancelled while converting")

            with (
                patch.object(
                    pipeline,
                    "resolve_managed_transcription_media",
                    return_value=ManagedMediaValidation(
                        True,
                        managed,
                        managed.stat().st_size,
                    ),
                ),
                patch.object(pipeline, "resolve_source", side_effect=resolve),
                patch.object(pipeline, "to_wav_16k_mono", side_effect=convert),
            ):
                with self.assertRaises(Exception) as raised:
                    pipeline.transcribe_source(
                        "https://example.test/work/cancel",
                        execution_policy=policy,
                        is_cancelled=lambda: cancelled,
                    )

            self.assertTrue(pipeline.was_cancelled(raised.exception))
            self.assertEqual(sources, [str(managed)])
            self.assertTrue(managed.exists())




if __name__ == "__main__":
    unittest.main()
