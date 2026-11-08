"""麦克风录音控制器：用 Qt 自带 QtMultimedia 录到本地文件（零额外依赖）。

录完交给现有转写管线（任意本地模型）。录音是纯本地操作，不联网。
"""

from __future__ import annotations

import time

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
        self._recorder.durationChanged.connect(self._on_duration)
        self._recorder.recorderStateChanged.connect(self._on_state)
        self._recorder.errorOccurred.connect(self._on_error)
        self._path: str | None = None
        self._emitted = False
        self.capture_started_monotonic: float | None = None
        self._stop_requested_monotonic: float | None = None

    def has_input(self) -> bool:
        from PySide6.QtMultimedia import QMediaDevices
        return bool(QMediaDevices.audioInputs())

    def start(self, path: str) -> None:
        self._path = path
        self._emitted = False
        self.capture_started_monotonic = None
        self._stop_requested_monotonic = None
        self._recorder.setOutputLocation(QUrl.fromLocalFile(path))
        self._recorder.record()

    def stop(self) -> None:
        if self._stop_requested_monotonic is None:
            self._stop_requested_monotonic = time.monotonic()
        self._recorder.stop()

    def _on_duration(self, ms: int) -> None:
        if ms > 0:
            # Qt's RecordingState can precede actual audio capture. Infer the
            # media origin from captured duration, not the record() call. Late
            # UI/encoder callbacks must not move that origin forward.
            observed = (self._stop_requested_monotonic if self._stop_requested_monotonic is not None
                        else time.monotonic())
            origin = observed - ms / 1000.0
            if self.capture_started_monotonic is None or origin < self.capture_started_monotonic:
                self.capture_started_monotonic = origin
        self.tick.emit(int(ms) // 1000)

    def is_recording(self) -> bool:
        return self._recorder.recorderState() == QMediaRecorder.RecorderState.RecordingState

    def _on_state(self, state) -> None:
        if state == QMediaRecorder.RecorderState.StoppedState and self._path and not self._emitted:
            self._emitted = True
            if self._recorder.duration() <= 0:
                self.failed.emit("没有采集到有效录音，请检查麦克风后重试。")
                return
            self._on_duration(self._recorder.duration())
            loc = self._recorder.actualLocation().toLocalFile() or self._path
            self.finished.emit(loc)

    def _on_error(self, error, msg) -> None:
        if error != QMediaRecorder.Error.NoError and not self._emitted:
            self._emitted = True
            self.failed.emit(msg or "录音失败（可能是麦克风权限被拒）")
