"""Internal-test GUI entry. This module is excluded from external candidates."""

from __future__ import annotations

import sys

from .internal_bootstrap import lock_internal_root


def main() -> int:
    # This must remain the first product action. No config, keyring, license,
    # library or UI module may be imported before the root is locked.
    root = lock_internal_root()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    from .config import AppConfig
    from .ui import theme
    from .ui.macos import activate_regular_app, unify_titlebar
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Lulu Internal Test")
    app.setApplicationDisplayName("Lulu Internal Test")
    theme.apply_fonts(app)
    cfg = AppConfig.load()
    cfg.auto_update_enabled = False
    cfg.last_output_dir = str(root / "output")
    cfg.media_dir = str(root / "output")
    cfg.recording_dir = str(root / "output")
    cfg.tts_output_dir = str(root / "output")
    cfg.voice_library_dir = str(root / "voices")
    cfg.tts_model_dir = str(root / "tts")
    cfg.save()
    theme.apply_theme(cfg.theme)
    activate_regular_app()

    window = MainWindow()
    window.setWindowTitle("Lulu · 内部测试版")
    badge = QLabel("内部测试版 · 临时隔离数据", window)
    badge.setObjectName("internalTestBadge")
    badge.setStyleSheet(
        "QLabel#internalTestBadge{background:#B45309;color:white;border-radius:10px;"
        "padding:5px 10px;font-weight:700;}"
    )
    badge.adjustSize()
    badge.move(max(12, window.width() - badge.width() - 18), 12)
    badge.raise_()
    app._lulu_main_window = window
    app._lulu_internal_badge = badge
    window.show()
    window.raise_()
    window.activateWindow()
    if unify_titlebar(window):
        window.enable_titlebar_inset()
    QTimer.singleShot(0, badge.raise_)
    return app.exec()
