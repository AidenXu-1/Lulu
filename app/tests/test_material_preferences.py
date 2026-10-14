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

    def test_complete_snapshot_is_immutable_and_broadcast_after_one_save(self):
        cfg = FakeConfig()
        service = MaterialPreferenceService(cfg)
        seen = []
        service.subscribe(seen.append)

        receipt = service.set(
            download_mode="video", save_media=True, save_cover=True
        )

        self.assertTrue(receipt.success)
        self.assertEqual(cfg.saves, 1)
        self.assertEqual(
            seen,
            [MaterialPreferenceSnapshot("video", True, True)],
        )
        self.assertEqual(
            cfg.collect_cover_contract_version,
            COLLECT_COVER_CONTRACT_VERSION,
        )
        with self.assertRaises(Exception):
            seen[0].save_media = False

    def test_each_save_failure_restores_complete_raw_triple_and_marker(self):
        for change in (
            {"download_mode": "video"},
            {"save_media": True},
            {"save_cover": True},
        ):
            with self.subTest(change=change):
                cfg = FakeConfig()
                cfg.collect_cover_contract_version = 77
                before = (
                    cfg.link_media_mode,
                    cfg.keep_media,
                    cfg.collect_cover,
                    cfg.collect_cover_contract_version,
                )
                cfg.save_result = False
                service = MaterialPreferenceService(cfg)
                seen = []
                service.subscribe(seen.append)

                receipt = service.set(**change)

                self.assertFalse(receipt.success)
                self.assertEqual(
                    (
                        cfg.link_media_mode,
                        cfg.keep_media,
                        cfg.collect_cover,
                        cfg.collect_cover_contract_version,
                    ),
                    before,
                )
                self.assertEqual(seen, [])

    def test_subscriber_failure_does_not_rollback_saved_truth(self):
        cfg = FakeConfig()
        service = MaterialPreferenceService(cfg)
        service.subscribe(lambda _snapshot: (_ for _ in ()).throw(RuntimeError()))

        receipt = service.set(save_media=True)

        self.assertTrue(receipt.success)
        self.assertEqual(receipt.notification_failures, 1)
        self.assertTrue(service.snapshot().save_media)

    def test_transcription_only_forces_material_effective_values_off(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:1",
            snapshot=MaterialPreferenceSnapshot("video", True, True),
        )
        self.assertEqual(policy.media_destination, "temporary_only")
        self.assertEqual(policy.material_snapshot.download_mode, "audio")
        self.assertFalse(policy.material_snapshot.save_media)
        self.assertFalse(policy.material_snapshot.save_cover)

    def test_transcription_only_runtime_never_changes_media_column(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:1",
            snapshot=MaterialPreferenceSnapshot("audio", True, True),
        )
        for state, message, transcript in (
            (TaskState.QUEUED, "排队中", "queued"),
            (TaskState.RUNNING, "正在下载临时音频", "queued"),
            (TaskState.RUNNING, "正在转录", "transcribing"),
            (TaskState.FAILED, "转录失败", "failed"),
        ):
            with self.subTest(state=state, message=message):
                task = Task(
                    1,
                    "作品",
                    "https://example.test/1",
                    "link",
                    state=state,
                    message=message,
                    stable_key="douyin:1",
                    execution_policy=policy,
                )
                runtime = derive_collection_item_runtime([task])["douyin:1"]
                self.assertEqual(runtime.transcript, transcript)
                self.assertEqual(runtime.media, "")

    def test_task_policy_rejects_homepage_without_stable_identity(self):
        with self.assertRaises(ValueError):
            TaskExecutionPolicy(
                origin="homepage_collect",
                mode="materialize",
                stable_key="",
                material_snapshot=MaterialPreferenceSnapshot(),
                media_destination="managed_collection",
            )

    def test_four_origin_policy_matrix_is_explicit_and_immutable(self):
        snapshot = MaterialPreferenceSnapshot("video", True, True)
        cases = (
            ("single_link", "", "materialize", "user_directory"),
            ("homepage_collect", "douyin:1", "materialize", "managed_collection"),
            ("homepage_history_material", "douyin:1", "materialize", "managed_collection"),
        )
        for origin, stable_key, mode, destination in cases:
            with self.subTest(origin=origin):
                policy = TaskExecutionPolicy(
                    origin=origin,
                    mode=mode,
                    stable_key=stable_key,
                    material_snapshot=snapshot,
                    media_destination=destination,
                )
                self.assertEqual(policy.media_destination, destination)
                self.assertTrue(policy.material_snapshot.save_media)
                with self.assertRaises(Exception):
                    policy.mode = "transcription_only"
        batch = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:1",
            snapshot=snapshot,
        )
        self.assertEqual(batch.media_destination, "temporary_only")

    def test_restart_load_normalizes_and_restores_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as raw, patch(
            "src.config._CONFIG_PATH", Path(raw) / "config.json"
        ):
            cfg = AppConfig()
            cfg.link_media_mode = "video"
            cfg.keep_media = True
            cfg.collect_cover = True
            cfg.collect_cover_contract_version = COLLECT_COVER_CONTRACT_VERSION
            self.assertTrue(cfg.save())
            loaded = AppConfig.load()
            self.assertEqual(
                MaterialPreferenceService(loaded).snapshot(),
                MaterialPreferenceSnapshot("video", True, True),
            )

    def test_transcription_only_pipeline_never_persists_media_or_cover(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:1",
            snapshot=MaterialPreferenceSnapshot("video", True, True),
        )

        class Engine:
            display_name = "Fake"

            def transcribe(self, _path, _progress):
                return Transcript("ok")

        class CoverWorkflow:
            def localize(self, **_kwargs):
                raise AssertionError("transcription_only must not localize cover")

        def resolve(_source, tmp, *_args, **kwargs):
            self.assertEqual(kwargs["link_media_mode"], "audio")
            media = Path(tmp) / "temporary.mp4"
            media.write_bytes(b"temporary")
            return media

        with (
            patch.object(
                pipeline,
                "resolve_managed_transcription_media",
                return_value=ManagedMediaValidation(
                    False,
                    reason_code="media_record_missing",
                ),
            ),
            patch.object(pipeline, "resolve_source", side_effect=resolve),
            patch.object(pipeline, "to_wav_16k_mono", side_effect=lambda media, _out: media),
            patch.object(pipeline, "get_engine", return_value=Engine()),
            patch.object(
                pipeline,
                "_save_media",
                side_effect=AssertionError("transcription_only must not persist media"),
            ),
        ):
            transcript = pipeline.transcribe_source(
                "https://example.test/work/1",
                keep_media=True,
                media_dir="/tmp/forbidden-user-media",
                cover_workflow=CoverWorkflow(),
                execution_policy=policy,
            )
        self.assertIsNone(transcript.saved_media)

    def test_single_link_policy_overrides_later_subtitle_preference(self):
        policy = TaskExecutionPolicy(
            origin="single_link",
            mode="materialize",
            stable_key="",
            material_snapshot=MaterialPreferenceSnapshot("video", True, False),
            media_destination="user_directory",
            media_directory="/tmp/frozen-media",
            prefer_subtitles=True,
        )
        subtitle = Transcript("平台字幕")

        with (
            patch("src.core.subtitles.fetch_from_link", return_value=subtitle) as fetch,
            patch.object(
                pipeline,
                "resolve_source",
                side_effect=AssertionError("命中冻结字幕偏好后不应下载媒体"),
            ),
        ):
            result = pipeline.transcribe_source(
                "https://example.test/work/subtitle",
                prefer_subtitles=False,
                execution_policy=policy,
            )

        fetch.assert_called_once()
        self.assertIs(result, subtitle)
        self.assertEqual(result.source, "https://example.test/work/subtitle")

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

            def convert(source, output):
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

    def test_batch_transcription_local_conversion_failure_falls_back_once(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:fallback",
            snapshot=MaterialPreferenceSnapshot("video", True, True),
        )

        class Engine:
            display_name = "Fake"

            def transcribe(self, _path, _progress):
                return Transcript("ok")

        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw) / "managed.mp4"
            managed.write_bytes(b"managed-video")
            sources = []
            modes = []
            conversions = []

            def resolve(source, tmp, *_args, **kwargs):
                sources.append(source)
                modes.append(kwargs["link_media_mode"])
                if source == str(managed):
                    return managed
                downloaded = Path(tmp) / "downloaded.m4a"
                downloaded.write_bytes(b"downloaded")
                return downloaded

            def convert(source, output):
                conversions.append(source)
                if source == managed:
                    raise OSError("managed file disappeared")
                output.write_bytes(b"wav")
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
                ),
                patch.object(pipeline, "resolve_source", side_effect=resolve),
                patch.object(pipeline, "to_wav_16k_mono", side_effect=convert),
                patch.object(pipeline, "get_engine", return_value=Engine()),
            ):
                transcript = pipeline.transcribe_source(
                    "https://example.test/work/fallback",
                    execution_policy=policy,
                )

            self.assertEqual(
                sources,
                [str(managed), "https://example.test/work/fallback"],
            )
            self.assertEqual(modes, ["audio", "audio"])
            self.assertEqual(len(conversions), 2)
            self.assertEqual(transcript.text, "ok")
            self.assertTrue(managed.exists())

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

            def convert(_source, _output):
                nonlocal cancelled
                cancelled = True
                raise OSError("cancelled while converting")

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

    def test_batch_transcription_engine_failure_does_not_download_again(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:engine-fail",
            snapshot=MaterialPreferenceSnapshot("audio", False, False),
        )

        class Engine:
            display_name = "Fake"

            def transcribe(self, _path, _progress):
                raise RuntimeError("engine failed")

        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw) / "managed.m4a"
            managed.write_bytes(b"managed-audio")
            sources = []

            def resolve(source, *_args, **_kwargs):
                sources.append(source)
                return managed

            def convert(_source, output):
                output.write_bytes(b"wav")
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
                ),
                patch.object(pipeline, "resolve_source", side_effect=resolve),
                patch.object(pipeline, "to_wav_16k_mono", side_effect=convert),
                patch.object(pipeline, "get_engine", return_value=Engine()),
            ):
                with self.assertRaisesRegex(RuntimeError, "engine failed"):
                    pipeline.transcribe_source(
                        "https://example.test/work/engine-fail",
                        execution_policy=policy,
                    )

            self.assertEqual(sources, [str(managed)])

    def test_batch_transcription_retry_rechecks_newly_available_managed_media(self):
        policy = TaskExecutionPolicy.transcription_only(
            origin="homepage_batch_transcribe",
            stable_key="douyin:retry",
            snapshot=MaterialPreferenceSnapshot("audio", False, False),
        )

        class Engine:
            display_name = "Fake"

            def transcribe(self, _path, _progress):
                return Transcript("ok")

        with tempfile.TemporaryDirectory() as raw:
            managed = Path(raw) / "managed.m4a"
            managed.write_bytes(b"managed-audio")
            sources = []

            def resolve(source, tmp, *_args, **_kwargs):
                sources.append(source)
                if source == str(managed):
                    return managed
                downloaded = Path(tmp) / "downloaded.m4a"
                downloaded.write_bytes(b"downloaded")
                return downloaded

            def convert(_source, output):
                output.write_bytes(b"wav")
                return output

            with (
                patch.object(
                    pipeline,
                    "resolve_managed_transcription_media",
                    side_effect=(
                        ManagedMediaValidation(
                            False,
                            reason_code="media_not_downloaded",
                        ),
                        ManagedMediaValidation(
                            True,
                            managed,
                            managed.stat().st_size,
                        ),
                    ),
                ) as lookup,
                patch.object(pipeline, "resolve_source", side_effect=resolve),
                patch.object(pipeline, "to_wav_16k_mono", side_effect=convert),
                patch.object(pipeline, "get_engine", return_value=Engine()),
            ):
                first = pipeline.transcribe_source(
                    "https://example.test/work/retry",
                    execution_policy=policy,
                )
                second = pipeline.transcribe_source(
                    "https://example.test/work/retry",
                    execution_policy=policy,
                )

            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(
                sources,
                ["https://example.test/work/retry", str(managed)],
            )
            self.assertEqual((first.text, second.text), ("ok", "ok"))


if __name__ == "__main__":
    unittest.main()
