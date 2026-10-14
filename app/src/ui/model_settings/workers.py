"""Model operations own their threads until work has actually settled."""
from PySide6.QtCore import QThread, Signal
from ...core import models_manager as mm


class ReadWorker(QThread):
    result = Signal(object, str)

    def __init__(self, read):
        super().__init__()
        self.read = read

    def request_cancel(self):
        self.requestInterruption()

    def run(self):
        try:
            value = self.read()
        except Exception:
            self.result.emit(None, '操作未完成，原设置已保留。请检查后重试。')
        else:
            if not self.isInterruptionRequested():
                self.result.emit(value, '')


class DownloadWorker(QThread):
    progress = Signal(float)
    progress_detail = Signal(str, float)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, model_id, *, observation_interval=None):
        super().__init__()
        self.model_id = model_id
        self._observation_interval = observation_interval
        self._cancel_requested = False

    def request_cancel(self):
        self._cancel_requested = True
        self.requestInterruption()

    def run(self):
        from ...core import model_transfer as transfer
        cancelled = lambda: self._cancel_requested or self.isInterruptionRequested()
        def check():
            if cancelled():
                raise mm.DownloadCancelled()
        try:
            with transfer.Session(self.progress_detail.emit, check, interval=self._observation_interval or .25) as session:
                transfer.prepare_asr(session, self.model_id)
                if not mm.is_downloaded(self.model_id):
                    mm.download_blocking(self.model_id, progress=lambda _f: None, is_cancelled=cancelled)
        except mm.DownloadCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished_ok.emit()
