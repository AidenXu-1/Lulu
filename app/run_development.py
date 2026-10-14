"""Local source UI entry for the developer's desktop launcher."""

import multiprocessing
import sys


def main() -> int:
    if getattr(sys, "frozen", False):
        raise RuntimeError("开发入口仅用于运行源码")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from src.ui import theme
    from src.ui.main_window import MainWindow
    from src.ui.macos import activate_regular_app, unify_titlebar

    app = QApplication(sys.argv)
    app.setApplicationName("Lulu 开发测试版")
    app.setApplicationDisplayName("Lulu 开发测试版")
    theme.apply_fonts(app)
    activate_regular_app()
    window = MainWindow()
    window.cfg.auto_update_enabled = False
    window.setWindowTitle("Lulu · 开发测试版（当前源码）")
    app._lulu_main_window = window

    def present() -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    present()
    if unify_titlebar(window):
        window.enable_titlebar_inset()
    for delay in (0, 250, 1000, 2000):
        QTimer.singleShot(delay, present)
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
