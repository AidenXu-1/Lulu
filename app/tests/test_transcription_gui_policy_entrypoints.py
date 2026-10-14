from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src import main as app_main
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.task import Task, TaskState
from src.ui.main_window import MainWindow

from src.core.auth_guard import OpenProductAccessGuard


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _PumpWorker:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.progress = _Signal()
        self.finished_ok = _Signal()
        self.failed = _Signal()
        self.cancelled = _Signal()
        self.finished = _Signal()
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True


class Phase2GuiAuthorizationEntrypointTests(unittest.TestCase):
    def test_main_window_declares_and_stores_unified_guard(self):
        signature = inspect.signature(MainWindow.__init__)
        parameters = tuple(signature.parameters)
        self.assertIn("local_file_transcription_authorization_guard", parameters)
        self.assertEqual(
            parameters[-1],
            "local_file_transcription_authorization_guard",
        )
        parameter = signature.parameters["local_file_transcription_authorization_guard"]
        self.assertIsNone(parameter.default)
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        source = inspect.getsource(MainWindow.__init__)
        self.assertIn(
            "self._local_file_transcription_authorization_guard =",
            source,
        )
        self.assertNotIn("local_file_transcription_authorization_policy", parameters)

    def test_main_window_has_no_license_return_to_gate_flow(self):
        self.assertFalse(hasattr(MainWindow, "_handle_license_deactivated"))
        parameters = inspect.signature(MainWindow.__init__).parameters
        self.assertNotIn("license_service", parameters)
        self.assertNotIn("on_license_deactivated", parameters)

    def test_pump_passes_the_same_guard_to_transcribe_worker(self):
        _PumpWorker.instances.clear()
        guard = object()
        task = SimpleNamespace(
            id=1,
            state=TaskState.QUEUED,
            source="/tmp/phase1a.wav",
            execution_policy=None,
            message="",
            progress=0.0,
        )
        shell = SimpleNamespace(
            current=None,
            tasks=[task],
            cards={},
            _paused_task_group_ids=set(),
            _deleting_task_group_ids=set(),
            _cover_preferences=object(),
            _cover_localizer=object(),
            _local_file_transcription_authorization_guard=guard,
            cfg=SimpleNamespace(
                engine_name="local_sensevoice",
                enable_link_download=True,
                keep_media=False,
                media_dir=None,
                link_media_mode="audio",
                prefer_subtitles=False,
            ),
            _refresh_badge=lambda: None,
            _on_progress=lambda *_args: None,
            _on_done=lambda *_args: None,
            _on_failed=lambda *_args: None,
            _on_cancelled=lambda *_args: None,
            _on_transcribe_worker_finished=lambda *_args: None,
        )
        with patch("src.ui.worker.TranscribeWorker", _PumpWorker), patch.object(
            MainWindow, "_task_group_id_for_task", return_value=None
        ), patch.object(
            MainWindow, "_freeze_task_policy", return_value=None
        ), patch.object(
            MainWindow, "_persist_task_queue", return_value=True
        ), patch.object(
            MainWindow, "_sync_runtime_preferences", return_value=None
        ):
            MainWindow._pump(shell)

        self.assertEqual(len(_PumpWorker.instances), 1)
        worker = _PumpWorker.instances[0]
        self.assertIs(
            worker.kwargs["local_file_transcription_authorization_guard"],
            guard,
        )
        self.assertTrue(worker.started)

    def test_closed_workbench_stops_local_file_before_file_access(self):
        guard = OpenProductAccessGuard()
        guard.revoke()
        task = Task(
            id=1,
            title="revoked local file",
            source="/definitely/missing/revoked-main-window.wav",
            kind="file",
            state=TaskState.QUEUED,
        )
        events = {"failed": [], "finished": [], "cancelled": []}
        shell = SimpleNamespace(
            current=None,
            tasks=[task],
            cards={},
            _paused_task_group_ids=set(),
            _deleting_task_group_ids=set(),
            _cover_preferences=object(),
            _cover_localizer=object(),
            _local_file_transcription_authorization_guard=guard,
            _material_snapshot=lambda: MaterialPreferenceSnapshot(
                "audio", False, False
            ),
            cfg=SimpleNamespace(
                engine_name="local_sensevoice",
                enable_link_download=True,
                keep_media=False,
                media_dir="/tmp/lulu-phase4-r1",
                link_media_mode="audio",
                prefer_subtitles=False,
            ),
            _refresh_badge=lambda: None,
            _on_progress=lambda *_args: None,
            _on_done=lambda *_args: events["finished"].append(True),
            _on_failed=lambda _tid, message: events["failed"].append(message),
            _on_cancelled=lambda *_args: events["cancelled"].append(True),
            _on_transcribe_worker_finished=lambda *_args: None,
        )

        def run_synchronously(worker) -> None:
            worker.run()

        with patch.object(
            MainWindow, "_task_group_id_for_task", return_value=None
        ), patch.object(
            MainWindow, "_persist_task_queue", return_value=True
        ), patch.object(
            MainWindow, "_sync_runtime_preferences", return_value=None
        ), patch(
            "src.ui.worker.TranscribeWorker.start", run_synchronously
        ), patch(
            "src.ui.worker.transcribe_source",
            side_effect=AssertionError("legacy transcribe_source reached"),
        ) as legacy, patch(
            "pathlib.Path.stat",
            side_effect=AssertionError("source stat before authorization"),
        ), patch(
            "pathlib.Path.open",
            side_effect=AssertionError("source open before authorization"),
        ):
            MainWindow._pump(shell)

        self.assertIsNotNone(task.execution_policy)
        self.assertEqual(len(events["failed"]), 1)
        self.assertEqual(events["failed"][0], "当前工作台不可用，请重新打开 Lulu 后重试。")
        self.assertEqual(events["finished"], [])
        self.assertEqual(events["cancelled"], [])
        legacy.assert_not_called()

    def test_formal_main_opens_without_license_service(self):
        from src.core.auth_guard import OpenProductAccessGuard

        built_windows = []

        class App:
            last = None

            def __init__(self, _argv):
                type(self).last = self

            def setApplicationName(self, _name):  # noqa: N802
                return None

            def setApplicationDisplayName(self, _name):  # noqa: N802
                return None

            @staticmethod
            def exec():
                return 0

            def quit(self):
                return None

        class Window:
            def __init__(
                self,
                *,
                defer_core_startup,
                local_file_transcription_authorization_guard,
            ):
                self.defer_core_startup = defer_core_startup
                self.guard = local_file_transcription_authorization_guard
                built_windows.append(self)

            @staticmethod
            def enable_titlebar_inset():
                return None

        with patch("PySide6.QtWidgets.QApplication", App), patch(
            "PySide6.QtCore.QTimer.singleShot", lambda *_args: None
        ), patch(
            "src.core.license.external_service.build_external_service",
            side_effect=AssertionError("retired license service must not be built"),
        ), patch(
            "src.ui.main_window.MainWindow", Window
        ), patch(
            "src.ui.macos.activate_regular_app", lambda: None
        ), patch(
            "src.ui.macos.unify_titlebar", lambda _window: False
        ), patch(
            "src.ui.theme.apply_fonts", lambda _app: None
        ), patch.object(
            app_main, "_apply_startup_theme", lambda: None
        ), patch.object(
            app_main, "_present_main_window", lambda _window: None
        ):
            self.assertEqual(app_main.main(), 0)

        self.assertEqual(len(built_windows), 1)
        self.assertFalse(built_windows[0].defer_core_startup)
        guard = built_windows[0].guard
        self.assertIs(type(guard), OpenProductAccessGuard)
        guard.assert_local_file_transcription_allowed(object())

    def test_trusted_and_internal_entrypoints_omit_unified_guard(self):
        workspace = Path(__file__).resolve().parents[2]
        for relative_path in ("app/src/trusted_main.py", "app/src/internal_main.py"):
            source = (workspace / relative_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
            main_window_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (node.func.id == "MainWindow" or (node.func.id == "_marked_call"
                     and any(isinstance(arg, ast.Name) and arg.id == "MainWindow" for arg in node.args)))
            ]
            self.assertTrue(main_window_calls, relative_path)
            for call in main_window_calls:
                with self.subTest(path=relative_path, line=call.lineno):
                    self.assertNotIn(
                        "local_file_transcription_authorization_guard",
                        {keyword.arg for keyword in call.keywords},
                    )

    def test_open_policy_is_wired_into_formal_gui_and_core(self):
        workspace = Path(__file__).resolve().parents[2]
        callsites = []
        for path in (workspace / "app/src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name == "OpenProductAccessGuard":
                    callsites.append((path.relative_to(workspace).as_posix(), node.lineno))
        self.assertEqual({path for path, _line in callsites}, {
            "app/src/main.py", "app/src/core/cli_runtime/factory.py",
        })


if __name__ == "__main__":
    unittest.main()
