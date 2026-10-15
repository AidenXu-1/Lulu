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


if __name__ == "__main__":
    unittest.main()
