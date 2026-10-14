"""Trusted-test GUI entry using the user's real HOME without authorization."""

from __future__ import annotations

import sys
from typing import Callable, TypeVar

from .release_features import load_sealed_features


_Result = TypeVar("_Result")
StageMarker = Callable[[str, str], None]


def _marked_call(
    marker: StageMarker,
    phase: str,
    success_event: str,
    action: Callable[[], _Result],
) -> _Result:
    marker(phase, "enter")
    try:
        result = action()
    except BaseException:
        try:
            marker(phase, "error")
        except BaseException:
            pass
        raise
    marker(phase, success_event)
    return result


def main(stage_marker: StageMarker) -> int:
    stage_marker("trusted_main", "enter")
    # The feature manifest is the first product action. Importing UI or user
    # configuration before this gate would allow a malformed package to run.
    _marked_call(
        stage_marker,
        "feature_gate",
        "ok",
        lambda: load_sealed_features(expected_profile="trusted_test"),
    )

    stage_marker("qt_import", "enter")
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QLabel
    except BaseException:
        try:
            stage_marker("qt_import", "error")
        except BaseException:
            pass
        raise
    stage_marker("qt_import", "ok")

    stage_marker("product_import", "enter")
    try:
        from .config import AppConfig
        from .ui import theme
        from .ui.macos import activate_regular_app, unify_titlebar
        from .ui.main_window import MainWindow
    except BaseException:
        try:
            stage_marker("product_import", "error")
        except BaseException:
            pass
        raise
    stage_marker("product_import", "ok")

    def create_application():
        application = QApplication(sys.argv)
        application.setApplicationName("Lulu Trusted Test")
        application.setApplicationDisplayName("Lulu Trusted Test")
        return application

    app = _marked_call(stage_marker, "qapplication", "ok", create_application)

    def configure_theme():
        theme.apply_fonts(app)
        config = AppConfig.load()
        theme.apply_theme(config.theme)
        return config

    cfg = _marked_call(
        stage_marker,
        "configuration_theme",
        "ok",
        configure_theme,
    )
    _marked_call(stage_marker, "activation", "ok", activate_regular_app)

    window = _marked_call(stage_marker, "main_window", "ok", MainWindow)
    # This is an in-memory runtime gate. It deliberately does not save over the
    # user's real config. Manual update checks also fail locally without a
    # packaged update key.

    def prepare_window():
        window.cfg.auto_update_enabled = False
        window.setWindowTitle("Lulu · 可信内测版")
        badge_value = QLabel("兆基本机自用测试版 · 不可外发", window)
        badge_value.setObjectName("trustedTestBadge")
        badge_value.setStyleSheet(
            "QLabel#trustedTestBadge{background:#9A3412;color:white;border-radius:10px;"
            "padding:5px 10px;font-weight:700;}"
        )
        badge_value.adjustSize()
        badge_value.move(max(12, window.width() - badge_value.width() - 18), 12)
        badge_value.raise_()
        app._lulu_main_window = window
        app._lulu_trusted_badge = badge_value
        return badge_value

    badge = _marked_call(
        stage_marker,
        "window_prepare",
        "ok",
        prepare_window,
    )

    def show_and_activate() -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    _marked_call(
        stage_marker,
        "show_activate",
        "issued",
        show_and_activate,
    )

    def configure_titlebar() -> None:
        if unify_titlebar(window):
            window.enable_titlebar_inset()
        QTimer.singleShot(0, badge.raise_)

    _marked_call(stage_marker, "titlebar", "ok", configure_titlebar)
    stage_marker("event_loop", "enter")
    try:
        result = app.exec()
    except BaseException:
        try:
            stage_marker("event_loop", "error")
        except BaseException:
            pass
        raise
    stage_marker("event_loop", "return")
    return result
