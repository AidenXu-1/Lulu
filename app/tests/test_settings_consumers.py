import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.ui.main_window import MainWindow
from src.ui.model_settings.controller import ModelController
from src.ui.voice_page import VoicePage
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.task import TaskExecutionPolicy, TaskState


class _Signal:
    def connect(self, _callback):
        return None


class _FakeWorker:
    created = None

    def __init__(self, source, engine_name, enable_link_download, **kwargs):
        self.source = source
        self.engine_name = engine_name
        self.enable_link_download = enable_link_download
        self.kwargs = kwargs
        self.progress = _Signal()
        self.finished_ok = _Signal()
        self.failed = _Signal()
        self.cancelled = _Signal()
        self.finished = _Signal()
        self.started = False
        _FakeWorker.created = self

    def start(self):
        self.started = True


class SettingsConsumerTests(unittest.TestCase):
    def test_model_and_voice_storage_entry_points_fail_closed_during_settings_migration(self):
        models = SimpleNamespace(closing=False, storage_busy=lambda: True, relocation_busy=lambda:False)
        voice = SimpleNamespace(
            main=SimpleNamespace(storage_relocation_active=lambda: True)
        )
        with self.assertRaisesRegex(RuntimeError, "正在迁移"):
            ModelController._gate(models)
        with patch("src.ui.voice_page.dialogs.alert") as voice_alert, \
                patch("src.ui.voice_page.QFileDialog.getExistingDirectory") as chooser:
            self.assertFalse(VoicePage._change_voice_library_dir(voice))
        voice_alert.assert_called_once()
        chooser.assert_not_called()

    def test_settings_storage_gate_disables_business_navigation_and_loaded_model_pages(self):
        class Enabled:
            def __init__(self):
                self.enabled = True

            def setEnabled(self, enabled):
                self.enabled = bool(enabled)

        shell = SimpleNamespace(
            _storage_relocation_in_progress=False,
            left_wrap=Enabled(),
            models_page=Enabled(),
            voice_page=Enabled(),
        )
        MainWindow._on_settings_storage_activity_changed(shell, True)
        self.assertTrue(shell._storage_relocation_in_progress)
        self.assertFalse(shell.left_wrap.enabled)
        self.assertFalse(shell.models_page.enabled)
        self.assertFalse(shell.voice_page.enabled)

        MainWindow._on_settings_storage_activity_changed(shell, False)
        self.assertFalse(shell._storage_relocation_in_progress)
        self.assertTrue(shell.left_wrap.enabled)
        self.assertTrue(shell.models_page.enabled)
        self.assertTrue(shell.voice_page.enabled)

    def test_runtime_preference_sync_drives_power_manager_without_business_ui(self):
        calls = []

        class Power:
            enabled = False

            def set_enabled(self, value):
                self.enabled = value
                calls.append(("enabled", value))

            def sync_activity(self, active, *, owner):
                calls.append(("active", active, owner))

        shell = SimpleNamespace(
            cfg=SimpleNamespace(prevent_sleep_during_tasks=True),
            power_manager=Power(),
            has_active_work=lambda: True,
        )
        MainWindow._sync_runtime_preferences(shell)
        self.assertEqual(calls, [("enabled", True), ("active", True, "main-window")])

    def test_completion_feedback_receives_real_target_and_frontmost_state(self):
        calls = []
        feedback = SimpleNamespace(
            success=lambda target, **kwargs: calls.append((target, kwargs))
        )
        shell = SimpleNamespace(
            completion_feedback=feedback,
            _window_is_active=False,
        )
        MainWindow._notify_task_completed(shell, page_index=4)
        self.assertEqual(calls[0][0].page_index, 4)
        self.assertEqual(calls[0][1], {"app_is_active": False})

    def test_failure_feedback_receives_frontmost_state_only(self):
        calls = []
        shell = SimpleNamespace(
            completion_feedback=SimpleNamespace(
                failure=lambda **kwargs: calls.append(kwargs)
            ),
            _window_is_active=True,
        )
        MainWindow._notify_task_failed(shell)
        self.assertEqual(calls, [{"app_is_active": True}])

    def test_running_exit_protection_can_keep_tasks_alive(self):
        event = SimpleNamespace(ignored=False, ignore=lambda: setattr(event, "ignored", True))
        shell = SimpleNamespace(
            library_page=None,
            cfg=SimpleNamespace(confirm_exit_during_tasks=True),
            has_active_work=lambda: True,
        )
        with patch("src.ui.main_window.dialogs.confirm", return_value=False):
            MainWindow.closeEvent(shell, event)
        self.assertTrue(event.ignored)

    def test_saved_directory_preferences_refresh_loaded_consumers(self):
        calls = []
        voice = SimpleNamespace(
            _update_output_dir_btn=lambda: calls.append("voice-index"),
            _update_minimax_output_dir_btn=lambda: calls.append("voice-minimax"),
        )
        shell = SimpleNamespace(
            cfg=SimpleNamespace(media_dir="/tmp/media", recording_dir="/tmp/recordings"),
            voice_page=voice,
            _publish_extraction_state=lambda: calls.append("extraction-state"),
        )

        MainWindow._sync_preferences_to_workbench(shell, "media_dir")
        MainWindow._sync_preferences_to_workbench(shell, "recording_dir")
        MainWindow._sync_preferences_to_workbench(shell, "tts_output_dir")

        self.assertEqual(
            calls,
            ["extraction-state", "extraction-state", "voice-index"],
        )

    def test_voice_and_model_directory_preferences_refresh_real_runtime_consumers(self):
        calls = []
        voice = SimpleNamespace(refresh=lambda: calls.append("voice-refresh"))
        models = SimpleNamespace(refresh=lambda: calls.append("models-refresh"))
        shell = SimpleNamespace(
            cfg=SimpleNamespace(
                voice_library_dir="/tmp/lulu-voices",
                models_dir="/tmp/lulu-models",
            ),
            voice_page=voice,
            models_page=models,
        )

        with patch("src.core.synthesis.voices.configure_voice_library_dir") as configure:
            MainWindow._sync_preferences_to_workbench(shell, "voice_library_dir")
        MainWindow._sync_preferences_to_workbench(shell, "models_dir")

        configure.assert_called_once_with("/tmp/lulu-voices")
        self.assertEqual(
            calls,
            ["voice-refresh", "models-refresh"],
        )

    def test_recording_path_reads_current_default_at_task_creation_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp) / "selected-recordings"
            shell = SimpleNamespace(
                cfg=SimpleNamespace(recording_dir=str(selected)),
                _rec_started_label="",
                _toast=lambda _message: None,
            )
            result = MainWindow._recording_path(shell, "录音")

            self.assertIsNotNone(result)
            self.assertEqual(Path(result).parent, selected)
            self.assertTrue(selected.is_dir())

    def test_export_dialog_reads_current_document_default_when_opened(self):
        selected = "/tmp/lulu-current-exports"
        task = SimpleNamespace(title="当前文稿", transcript=SimpleNamespace(text="正文"))
        shell = SimpleNamespace(
            cfg=SimpleNamespace(last_output_dir=selected),
            _task=lambda _task_id: task,
            _ensure_task_transcript=lambda *_: True,
            _export_task=lambda *_: None,
        )
        with patch("src.ui.main_window.QFileDialog.getSaveFileName", return_value=("", "")) as dialog:
            MainWindow._export_task(shell, 1)

        default_path = dialog.call_args.args[2]
        self.assertEqual(Path(default_path).parent, Path(selected))

    def test_new_transcription_worker_reads_current_media_default(self):
        task = SimpleNamespace(
            id=7,
            source="https://example.invalid/video",
            state=TaskState.QUEUED,
            message="",
            progress=0.0,
        )
        shell = SimpleNamespace(
            current=None,
            tasks=[task],
            cards={},
            cfg=SimpleNamespace(
                engine_name="sensevoice",
                enable_link_download=True,
                keep_media=True,
                media_dir="/tmp/lulu-current-media",
                link_media_mode="audio",
                prefer_subtitles=False,
            ),
            _cover_preferences=None,
            _cover_localizer=None,
            _on_progress=lambda *_args: None,
            _on_done=lambda *_args: None,
            _on_failed=lambda *_args: None,
            _on_cancelled=lambda *_args: None,
            _refresh_badge=lambda: None,
        )
        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            MainWindow._pump(shell)

        self.assertEqual(_FakeWorker.created.kwargs["media_dir"], "/tmp/lulu-current-media")
        self.assertTrue(_FakeWorker.created.started)

    def test_started_task_keeps_frozen_directory_and_subtitle_choice_while_queued(self):
        policy = TaskExecutionPolicy(
            origin="single_link",
            mode="materialize",
            stable_key="",
            material_snapshot=MaterialPreferenceSnapshot("video", True, True),
            media_destination="user_directory",
            media_directory="/tmp/lulu-frozen-media",
            prefer_subtitles=True,
        )
        task = SimpleNamespace(
            id=9,
            source="https://example.invalid/video",
            kind="link",
            state=TaskState.QUEUED,
            message="",
            progress=0.0,
            execution_policy=policy,
        )
        shell = SimpleNamespace(
            current=None,
            tasks=[task],
            cards={},
            cfg=SimpleNamespace(
                engine_name="sensevoice",
                enable_link_download=True,
                keep_media=False,
                media_dir="/tmp/lulu-later-media",
                link_media_mode="audio",
                prefer_subtitles=False,
            ),
            _cover_preferences=None,
            _cover_localizer=None,
            _on_progress=lambda *_args: None,
            _on_done=lambda *_args: None,
            _on_failed=lambda *_args: None,
            _on_cancelled=lambda *_args: None,
            _refresh_badge=lambda: None,
        )

        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            MainWindow._pump(shell)

        self.assertEqual(
            _FakeWorker.created.kwargs["media_dir"],
            "/tmp/lulu-frozen-media",
        )
        self.assertTrue(_FakeWorker.created.kwargs["prefer_subtitles"])
        frozen = _FakeWorker.created.kwargs["execution_policy"]
        self.assertEqual(frozen.media_directory, policy.media_directory)
        self.assertEqual(frozen.material_snapshot, policy.material_snapshot)
        self.assertEqual(frozen.prefer_subtitles, policy.prefer_subtitles)
        self.assertEqual(frozen.engine_name, "sensevoice")

    def test_task_options_are_read_when_start_is_clicked_not_when_link_is_grabbed(self):
        task = SimpleNamespace(kind="link", execution_policy=None)
        shell = SimpleNamespace(
            cfg=SimpleNamespace(
                link_media_mode="video",
                keep_media=True,
                collect_cover=True,
                engine_name="local_sensevoice",
                media_dir="/tmp/selected-before-start",
                prefer_subtitles=True,
            ),
            _material_snapshot=lambda: MaterialPreferenceSnapshot(
                "video", True, True
            ),
        )

        MainWindow._freeze_task_policy(shell, task)

        self.assertEqual(task.execution_policy.material_snapshot.download_mode, "video")
        self.assertTrue(task.execution_policy.material_snapshot.save_media)
        self.assertTrue(task.execution_policy.material_snapshot.save_cover)
        self.assertEqual(
            task.execution_policy.media_directory,
            "/tmp/selected-before-start",
        )
        self.assertTrue(task.execution_policy.prefer_subtitles)

    def test_failed_media_directory_commit_keeps_next_task_snapshot(self):
        task = SimpleNamespace(
            id=8,
            source="https://example.invalid/video",
            state=TaskState.QUEUED,
            message="",
            progress=0.0,
        )
        messages = []
        cfg = SimpleNamespace(
            engine_name="sensevoice",
            enable_link_download=True,
            keep_media=True,
            media_dir="/tmp/lulu-committed-media",
            link_media_mode="audio",
            prefer_subtitles=False,
            save=lambda: False,
        )
        shell = SimpleNamespace(
            current=None,
            tasks=[task],
            cards={},
            cfg=cfg,
            _cover_preferences=None,
            _cover_localizer=None,
            _sync_preferences_to_workbench=lambda field: MainWindow._sync_preferences_to_workbench(shell, field),
            _publish_extraction_state=lambda: None,
            _toast=messages.append,
            _on_progress=lambda *_args: None,
            _on_done=lambda *_args: None,
            _on_failed=lambda *_args: None,
            _on_cancelled=lambda *_args: None,
            _refresh_badge=lambda: None,
        )

        self.assertFalse(
            MainWindow._commit_quick_preference(
                shell,
                "media_dir",
                "/tmp/lulu-uncommitted-media",
            )
        )
        with patch("src.ui.worker.TranscribeWorker", _FakeWorker):
            MainWindow._pump(shell)

        self.assertEqual(cfg.media_dir, "/tmp/lulu-committed-media")
        self.assertEqual(
            _FakeWorker.created.kwargs["media_dir"],
            "/tmp/lulu-committed-media",
        )
        self.assertEqual(messages, ["设置没有保存，本次仍按原设置执行"])


if __name__ == "__main__":
    unittest.main()
