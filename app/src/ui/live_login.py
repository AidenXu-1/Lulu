"""One cancellable browser-login request; all user messages are secret-free."""
from PySide6.QtCore import QThread, Signal

from ..core.live_recording.contracts import LiveError


class LiveLoginRequest(QThread):
    received = Signal(str)
    notice = Signal(str)

    def run(self):
        try:
            from ..core.live_recording.browser_login import login_cookie
            from ..core.models_manager import app_data_dir
            value = login_cookie(app_data_dir(), is_cancelled=self.isInterruptionRequested,
                                 progress=self.notice.emit)
            if value and not self.isInterruptionRequested():
                self.received.emit(value)
        except LiveError as exc:
            if not self.isInterruptionRequested():
                self.notice.emit(str(exc))
        except Exception:
            if not self.isInterruptionRequested():
                self.notice.emit('登录未完成，原登录信息保留，请重试')
