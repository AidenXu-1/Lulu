"""One local, bounded waveform decoder shared by native and web players."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QObject, QProcess, QTimer, Signal


class AudioWaveform(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks = []
        self.state = 'idle'
        self._signature = None
        self._process = None
        self._bytes = bytearray()
        self._raw_peaks = []
        self._stride = 40
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(lambda: self._failed(self._process))

    @property
    def description(self):
        return {'idle': '选择音频后显示真实波形', 'missing': '音频文件不在原位置',
                'loading': '正在读取真实波形…', 'ready': '真实音频波形',
                'failed': '暂时无法读取波形，仍可使用播放器试听'}[self.state]

    def stop(self):
        self._timeout.stop()
        process, self._process = self._process, None
        if process is not None:
            # Finish asynchronously when switching files; don't stall editing.
            process.kill()
        self._bytes.clear()
        self.state = 'idle'

    def shutdown(self):
        self.stop()
        for process in self.findChildren(QProcess):
            if process.state() != QProcess.NotRunning:
                process.kill()
                process.waitForFinished(1000)

    def load_file(self, path):
        path = str(path or '')
        try:
            info = Path(path).stat() if path else None
            signature = (path, info.st_ino, info.st_size, info.st_mtime_ns) if info else None
        except OSError:
            signature = None
        if signature and signature == self._signature and self.state in {'ready', 'loading'}:
            return
        self.stop()
        self._signature = signature
        self.peaks, self._raw_peaks, self._stride = [], [], 40
        self.state = 'loading' if signature and Path(path).is_file() else ('missing' if path else 'idle')
        self.changed.emit()
        if self.state != 'loading':
            return
        import imageio_ffmpeg
        process = QProcess(self)
        self._process = process
        process.setStandardErrorFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.finished.connect(lambda code, _status: self._finished(process, code))
        process.errorOccurred.connect(lambda _error: self._failed(process))
        process.start(imageio_ffmpeg.get_ffmpeg_exe(), [
            '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe', '-i', path,
            '-vn', '-ac', '1', '-ar', '8000', '-f', 'f32le', 'pipe:1',
        ])
        self._timeout.start(120000)

    def _read(self, process):
        if process is not self._process:
            return True
        # Let Qt process input between chunks, including on very long recordings.
        for _ in range(4):
            if not process.bytesAvailable():
                return True
            self._bytes.extend(bytes(process.read(65536)))
            self._consume_samples()
        if process.bytesAvailable():
            QTimer.singleShot(0, lambda: self._read(process))
            return False
        return True

    def _consume_samples(self):
        import numpy as np
        while len(self._bytes) >= self._stride * 4:
            bins = min(len(self._bytes) // (self._stride * 4), 4096 - len(self._raw_peaks))
            size = bins * self._stride * 4
            samples = np.frombuffer(bytes(self._bytes[:size]), dtype='<f4')
            del self._bytes[:size]
            values = np.nan_to_num(np.abs(samples), nan=0, posinf=0, neginf=0)
            self._raw_peaks.extend(values.reshape(bins, self._stride).max(axis=1).tolist())
            if len(self._raw_peaks) == 4096:
                self._raw_peaks = np.asarray(self._raw_peaks).reshape(-1, 2).max(axis=1).tolist()
                self._stride *= 2

    def _failed(self, process):
        if process is not None and process is self._process:
            self.stop()
            if process.state() == QProcess.NotRunning:
                process.deleteLater()
            self.state = 'failed'
            self.changed.emit()

    def _finished(self, process, code):
        if process is not self._process:
            process.deleteLater()
            return
        if not self._read(process):
            QTimer.singleShot(0, lambda: self._finished(process, code))
            return
        import numpy as np
        tail = bytes(self._bytes[:len(self._bytes) // 4 * 4])
        if tail:
            self._raw_peaks.append(float(np.max(np.nan_to_num(
                np.abs(np.frombuffer(tail, dtype='<f4')), nan=0, posinf=0, neginf=0))))
        self._timeout.stop()
        self._process = None
        process.deleteLater()
        self._bytes.clear()
        self.state = 'failed' if code or not self._raw_peaks else 'ready'
        if self.state == 'ready':
            self.peaks = [float(np.max(part)) for part in
                          np.array_split(self._raw_peaks, min(160, len(self._raw_peaks)))]
            maximum = max(self.peaks, default=0)
            self.peaks = [(value / maximum) ** .75 if maximum else 0 for value in self.peaks]
        self.changed.emit()
