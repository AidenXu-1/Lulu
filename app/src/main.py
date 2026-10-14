"""应用入口。

运行(在 app/ 目录下):
    python -m src.main
"""

from __future__ import annotations

import sys
import os


def main() -> int:
    if '--lulu-update-recover' in sys.argv:
        from .core.updater.restart_recovery import run_recovery
        args = sys.argv[sys.argv.index('--lulu-update-recover') + 1:]
        result = run_recovery(*args) if len(args) == 4 else 2
        return _show_update_recovery_error() if result == 5 else result
    if "--lulu-update-helper" in sys.argv:
        from .core.updater.helper_entry import run_helper
        index=sys.argv.index("--lulu-update-helper")
        return run_helper(sys.argv[index+1]) if len(sys.argv)>index+1 else 2

    if getattr(sys, 'frozen', False) and '--lulu-update-ready' not in sys.argv:
        from .core.updater.restart_recovery import launch_pending_recovery
        try:
            if launch_pending_recovery():
                return 0
        except Exception as exc:
            # Without a verified installer state, a second GUI could revive
            # the old Core while its App is being replaced. Preserve the
            # transaction and stop before loading any UI or business runtime.
            from .core.diagnostics import record_failure
            record_failure('update_startup', exc)
            return _show_update_recovery_error()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .core.auth_guard import OpenProductAccessGuard
    from .ui import theme
    from .ui.main_window import MainWindow
    from .ui.macos import activate_regular_app

    app = QApplication(sys.argv)
    app.setApplicationName("Lulu")
    app.setApplicationDisplayName("Lulu")
    theme.apply_fonts(app)
    # 在创建控件前应用唯一的 Lulu 宣纸样式。
    _apply_startup_theme()

    activate_regular_app()

    def present_current_main_window(window) -> bool:
        if getattr(app, "_lulu_main_window", None) is not window:
            return False
        _present_main_window(window)
        return True

    def open_main(*, defer_core_startup=False) -> None:
        local_file_transcription_authorization_guard = OpenProductAccessGuard()
        window = MainWindow(
            defer_core_startup=defer_core_startup,
            local_file_transcription_authorization_guard=(
                local_file_transcription_authorization_guard
            ),
        )
        app._lulu_main_window = window  # keep a Python-side strong ref; otherwise macOS can end up with PID alive but no window
        present_current_main_window(window)
        # macOS:透明标题栏 + 内容延伸到顶(顶部与内容连通);成功则给顶部留出红绿灯空间
        from .ui.macos import unify_titlebar
        if unify_titlebar(window):
            window.enable_titlebar_inset()
            present_current_main_window(window)
        for delay_ms in (0, 250, 1000, 2000):
            QTimer.singleShot(
                delay_ms,
                lambda w=window: present_current_main_window(w),
            )
        QTimer.singleShot(2500, _run_startup_maintenance)
        _schedule_update_ready(window)

    from .core.agent_entry.standalone_bootstrap import start_preparation
    if not start_preparation(app,
            lambda: app._lulu_main_window.finish_install_preparation(True),
            on_start=lambda: open_main(defer_core_startup=True),
            on_failed=lambda: app._lulu_main_window.finish_install_preparation(False,
                reason=getattr(app, '_lulu_install_preparation_failure', None))):
        open_main()
    return app.exec()


def _schedule_update_ready(window) -> None:
    if "--lulu-update-ready" not in sys.argv:
        return
    import time
    from PySide6.QtCore import QTimer
    from .core.updater.startup import UpdateReadyGate

    gate = UpdateReadyGate()
    timer = QTimer(window)

    def check_ready():
        if gate.observe(window, now=time.monotonic()):
            if _write_update_ready_if_requested():
                timer.stop()

    timer.setInterval(200)
    timer.timeout.connect(check_ready)
    window._update_ready_timer = timer
    timer.start()


def _write_update_ready_if_requested() -> bool:
    if "--lulu-update-ready" not in sys.argv:
        return False
    index=sys.argv.index("--lulu-update-ready")
    if len(sys.argv)<=index+1:
        return False
    try:
        from pathlib import Path
        from .core.updater.install import InstallTransaction, write_ready
        from .core.updater.process_identity import current_identity
        plan_path=Path(sys.argv[index+1]).resolve()
        tx=InstallTransaction(plan_path.parents[2])
        plan=tx.load_plan(plan_path)
        started,executable=current_identity()
        write_ready(Path(plan["ready_path"]),plan,pid=os.getpid(),pid_start_time=started,executable_path=executable)
        return True
    except Exception:
        return False


def _apply_startup_theme():
    """Apply Lulu paper before the workbench, including legacy installations."""
    from .ui import theme

    return theme.apply_theme(theme.DEFAULT_THEME)


def _present_main_window(window) -> None:
    try:
        from PySide6.QtCore import Qt

        window.setWindowState(Qt.WindowActive)
    except Exception:  # noqa: BLE001
        pass
    window.show()
    window.showNormal()
    window.raise_()
    window.activateWindow()


def _run_startup_maintenance() -> None:
    # 这些维护动作不参与首屏绘制:先让窗口出现,再补 PATH 与临时目录清理。
    try:
        from .core.audio.converter import ensure_ffmpeg_on_path

        ensure_ffmpeg_on_path()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .core.pipeline import cleanup_stale_temp

        cleanup_stale_temp()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .core.feishu import BitableMediaReceiptStore

        BitableMediaReceiptStore().cleanup_expired_best_effort()
    except Exception:  # noqa: BLE001
        pass


def _show_update_recovery_error() -> int:
    """A visible repair exit, without starting Core against an uncertain App."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .core.updater.constants import MANUAL_DOWNLOAD_URL
    from .ui.macos import activate_regular_app
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName('Lulu')
    activate_regular_app()
    dialog = QMessageBox(QMessageBox.Icon.Warning, 'Lulu 更新需要恢复',
        '上次更新尚未恢复完成，原有文稿、模型、现有程序和恢复记录均已保留。\n'
        '请保留现场并联系作者处理恢复记录。可查看最新版安装包；仅覆盖安装可能仍需处理这份记录。')
    download = dialog.addButton('查看最新版安装包', QMessageBox.ButtonRole.ActionRole)
    dialog.addButton('关闭', QMessageBox.ButtonRole.RejectRole)
    dialog.exec()
    if dialog.clickedButton() is download:
        QDesktopServices.openUrl(QUrl(MANUAL_DOWNLOAD_URL))
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
