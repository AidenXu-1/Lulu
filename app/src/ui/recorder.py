"""麦克风录音控制器：用 Qt 自带 QtMultimedia 录到本地文件（零额外依赖）。

录完交给现有转写管线（任意本地模型）。录音是纯本地操作，不联网。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import (
    QAudioInput,
    QMediaCaptureSession,
    QMediaFormat,
    QMediaRecorder,
)


class MicRecorder(QObject):
    tick = Signal(int)       # 已录秒数
    finished = Signal(str)   # 录音文件路径
    failed = Signal(str)     # 出错（含没权限）

    def __init__(self):
        super().__init__()
        self._session = QMediaCaptureSession()
        self._input = QAudioInput()
        self._session.setAudioInput(self._input)
        self._recorder = QMediaRecorder()
        self._session.setRecorder(self._recorder)
        fmt = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
        fmt.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self._recorder.setMediaFormat(fmt)
        self._recorder.setQuality(QMediaRecorder.Quality.HighQuality)
        self._recorder.durationChanged.connect(lambda ms: self.tick.emit(int(ms) // 1000))
        self._recorder.recorderStateChanged.connect(self._on_state)
        self._recorder.errorOccurred.connect(self._on_error)
        self._path: str | None = None
        self._emitted = False

    def has_input(self) -> bool:
        from PySide6.QtMultimedia import QMediaDevices
        return bool(QMediaDevices.audioInputs())

    def start(self, path: str) -> None:
        self._path = path
        self._emitted = False
        self._recorder.setOutputLocation(QUrl.fromLocalFile(path))
        self._recorder.record()

    def stop(self) -> None:
        self._recorder.stop()

    def is_recording(self) -> bool:
        return self._recorder.recorderState() == QMediaRecorder.RecorderState.RecordingState

    def _on_state(self, state) -> None:
        if state == QMediaRecorder.RecorderState.StoppedState and self._path and not self._emitted:
            self._emitted = True
            loc = self._recorder.actualLocation().toLocalFile() or self._path
            self.finished.emit(loc)

    def _on_error(self, error, msg) -> None:
        if error != QMediaRecorder.Error.NoError:
            self.failed.emit(msg or "录音失败（可能是麦克风权限被拒）")
