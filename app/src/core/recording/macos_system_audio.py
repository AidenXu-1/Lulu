"""macOS ScreenCaptureKit system-audio recording backend."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .audio_tools import _wav_stats

from .base import (
    ProgressCallback,
    RecordingBackend,
    RecordingRequest,
    RecordingResult,
    RecordingSession,
    RecordingSource,
    RecordingTrack,
    RecordingTrackKind,
    _noop,
)


class RecordingBackendError(RuntimeError):
    """Recording backend failed before producing a usable recording."""


_SWIFT_HELPER = r'''
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

final class AudioCaptureDelegate: NSObject, SCStreamOutput {
    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private var started = false
    private(set) var sampleBufferCount = 0

    init(outputURL: URL) throws {
        writer = try AVAssetWriter(outputURL: outputURL, fileType: .m4a)
        input = AVAssetWriterInput(
            mediaType: .audio,
            outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 2,
                AVEncoderBitRateKey: 128_000,
            ]
        )
        input.expectsMediaDataInRealTime = true
        if writer.canAdd(input) {
            writer.add(input)
        } else {
            throw NSError(domain: "LuluSystemAudioCapture", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Cannot prepare system audio writer"
            ])
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }

        if !started {
            let firstPTS = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            writer.startWriting()
            writer.startSession(atSourceTime: firstPTS)
            started = true
            let origin = CMTimeGetSeconds(firstPTS)
            let hostNow = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
            if origin.isFinite && abs(hostNow - origin) < 5 {
                print("capture_started_monotonic=\(origin)")
                fflush(stdout)
            }
        }

        if input.isReadyForMoreMediaData, input.append(sampleBuffer) {
            sampleBufferCount += 1
        }
    }

    func finish() async throws {
        input.markAsFinished()
        await withCheckedContinuation { continuation in
            writer.finishWriting {
                continuation.resume()
            }
        }
        if writer.status == .failed {
            throw writer.error ?? NSError(domain: "LuluSystemAudioCapture", code: 3, userInfo: [
                NSLocalizedDescriptionKey: "System audio writer failed"
            ])
        }
    }
}

@main
struct SystemAudioCapture {
    static func main() async {
        do {
            try await run()
        } catch {
            let native = error as NSError
            let data = try? JSONSerialization.data(withJSONObject: ["domain": native.domain, "code": native.code])
            let detail = data.flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
            fputs("capture_error: \(detail)\n", stderr)
            exit(1)
        }
    }

    private static func run() async throws {
        guard CommandLine.arguments.count >= 4 else {
            fputs("usage: SystemAudioCapture <output.m4a> <stop-file> <max-seconds>\n", stderr)
            exit(64)
        }

        let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let stopURL = URL(fileURLWithPath: CommandLine.arguments[2])
        let maxSeconds = Double(CommandLine.arguments[3]) ?? 3600.0
        try? FileManager.default.removeItem(at: outputURL)
        try? FileManager.default.removeItem(at: stopURL)

        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "LuluSystemAudioCapture", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "No display available"
            ])
        }

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = max(display.width, 2)
        configuration.height = max(display.height, 2)
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = false
        configuration.sampleRate = 48_000
        configuration.channelCount = 2

        let capture = try AudioCaptureDelegate(outputURL: outputURL)
        let stream = SCStream(filter: filter, configuration: configuration, delegate: nil)
        let queue = DispatchQueue(label: "lulu.system-audio-capture")
        try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: queue)

        try await stream.startCapture()
        print("started")
        fflush(stdout)

        let deadline = Date().addingTimeInterval(max(maxSeconds, 1.0))
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: stopURL.path) {
                break
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }

        try await stream.stopCapture()
        try await capture.finish()
        print("sample_buffers=\(capture.sampleBufferCount)")
        print("output=\(outputURL.path)")
    }
}
'''


def _helper_root() -> Path:
    return Path(tempfile.gettempdir()) / "lulu-system-audio-helper"


def _helper_paths() -> tuple[Path, Path, Path]:
    digest = hashlib.sha256(_SWIFT_HELPER.encode("utf-8")).hexdigest()[:12]
    root = _helper_root() / digest
    return root / "SystemAudioCapture.swift", root / "SystemAudioCapture", root / "module-cache"


def _bundled_helper() -> Path | None:
    root = getattr(sys, "_MEIPASS", None)
    return Path(root) / "recording_runtime" / "SystemAudioCapture" if root else None


def ensure_macos_system_audio_helper(progress: ProgressCallback | None = None) -> Path:
    progress = progress or _noop
    bundled = _bundled_helper()
    if bundled is not None and bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    if getattr(sys, "frozen", False):
        raise RecordingBackendError("系统声音录制组件缺失，请重新安装完整的 Lulu。")

    source_path, binary_path, module_cache = _helper_paths()
    if binary_path.is_file() and os.access(binary_path, os.X_OK):
        return binary_path
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RecordingBackendError("系统声音录制组件不可用，请使用完整安装版 Lulu。")

    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_SWIFT_HELPER, encoding="utf-8")
    module_cache.mkdir(parents=True, exist_ok=True)
    progress("正在准备系统声音录制组件", -1)
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
    cmd = [
        swiftc,
        "-parse-as-library",
        str(source_path),
        "-framework",
        "ScreenCaptureKit",
        "-framework",
        "AVFoundation",
        "-framework",
        "CoreMedia",
        "-o",
        str(binary_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecordingBackendError("系统声音录制组件准备超时") from exc
    if completed.returncode != 0:
        raise RecordingBackendError("系统声音录制组件准备失败")
    return binary_path


def _reader_thread(stream, out: "queue.Queue[str]") -> None:
    try:
        for line in iter(stream.readline, ""):
            out.put(line.rstrip("\n"))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _capture_failure(stderr_queue: "queue.Queue[str]", stage: str) -> RecordingBackendError:
    domain, code = "", None
    while True:
        try:
            line = stderr_queue.get_nowait()
        except queue.Empty:
            break
        if not line.startswith("capture_error: "):
            continue
        try:
            error = json.loads(line.removeprefix("capture_error: "))
            domain, code = error.get("domain", ""), error.get("code")
        except (ValueError, AttributeError):
            continue
    if not isinstance(domain, str) or not isinstance(code, int):
        domain, code = "", None
    if domain == "com.apple.ScreenCaptureKit.SCStreamErrorDomain" and code == -3801:
        return RecordingBackendError("系统拒绝了 Lulu 的录屏与系统录音权限。请在系统设置中允许；若开关已开启，请关闭后重新开启，并重开 Lulu。")
    if (domain, code) in {("NSPOSIXErrorDomain", 28), ("NSCocoaErrorDomain", 640)}:
        return RecordingBackendError("磁盘空间不足，系统声音录音未保存。请腾出空间后重试。")
    if (domain, code) in {("NSPOSIXErrorDomain", 13), ("NSCocoaErrorDomain", 513)}:
        return RecordingBackendError("录音保存目录不可写，请重新选择保存位置。")
    suffix = f"（错误码 {code}）" if isinstance(code, int) else ""
    return RecordingBackendError(f"系统声音录制{stage}失败{suffix}。请重试或重新打开 Lulu。")


def _decode_to_wav(path: Path, wav_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        return False
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "48000",
            str(wav_path),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 44


class MacOSSystemAudioSession:
    def __init__(
        self,
        request: RecordingRequest,
        process: subprocess.Popen,
        stop_file: Path,
        stdout_queue: "queue.Queue[str]",
        stderr_queue: "queue.Queue[str] | None" = None,
        readers: tuple[threading.Thread, ...] = (),
    ):
        self._request = request
        self._process = process
        self._stop_file = stop_file
        self._stdout_queue = stdout_queue
        self._stderr_queue = stderr_queue if stderr_queue is not None else queue.Queue()
        self._readers = readers
        self._cancelled = False
        self._capture_started_monotonic: float | None = None

    def _observe_timing(self, line: str) -> None:
        prefix = "capture_started_monotonic="
        if line.startswith(prefix):
            try:
                value = float(line[len(prefix):])
            except ValueError:
                return
            if math.isfinite(value) and value >= 0 and self._capture_started_monotonic is None:
                self._capture_started_monotonic = value

    @property
    def request(self) -> RecordingRequest:
        return self._request

    def is_active(self) -> bool:
        return self._process.poll() is None

    def cancel(self) -> None:
        self._cancelled = True
        if self.is_active():
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._cleanup_stop_file()
        self._cleanup_output()

    def stop(self, progress: ProgressCallback | None = None) -> RecordingResult:
        progress = progress or _noop
        if self._cancelled:
            raise RecordingBackendError("录音已取消")
        self._stop_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_file.write_text("stop", encoding="utf-8")
        try:
            self._process.wait(timeout=12)
        except subprocess.TimeoutExpired as exc:
            self.cancel()
            raise RecordingBackendError("系统声音录制超时,已清理临时文件") from exc

        if self._process.returncode != 0:
            for reader in self._readers:
                reader.join(timeout=0.5)
            self._cleanup_stop_file()
            self._cleanup_output()
            raise _capture_failure(self._stderr_queue, "保存")

        output_path = self._request.output_path
        if not output_path.exists() or output_path.stat().st_size == 0:
            self._cleanup_stop_file()
            self._cleanup_output()
            raise RecordingBackendError("没有生成可用的系统声音录音")

        for reader in self._readers:
            reader.join(timeout=0.5)
        lines = list(self._drain_stdout())
        for line in lines:
            self._observe_timing(line)
        metadata: dict[str, object] = {"stdout": lines,
            "capture_started_monotonic": self._capture_started_monotonic}
        wav_path = output_path.with_suffix(".analysis.wav")
        is_silent: bool | None = None
        duration: float | None = None
        try:
            decoded = _decode_to_wav(output_path, wav_path)
        except (OSError, subprocess.TimeoutExpired):
            decoded = False
        if decoded:
            stats = _wav_stats(wav_path)
            metadata["analysis"] = stats
            duration = stats.get("duration")
            is_silent = stats.get("rms", 0.0) < 80.0 or stats.get("peak", 0.0) < 200.0
            try:
                wav_path.unlink()
            except OSError:
                pass
        else:
            self._cleanup_stop_file()
            self._cleanup_output()
            raise RecordingBackendError("系统声音录音无法读取，未加入转录队列。请重新录制。")
        if is_silent is True:
            self._cleanup_stop_file()
            self._cleanup_output()
            raise RecordingBackendError("没有录到清晰的系统声音,请先播放电脑声音后重试")

        track = RecordingTrack(
            kind=RecordingTrackKind.SYSTEM_AUDIO,
            path=output_path,
            duration_seconds=duration,
            sample_rate=48000,
            channels=2,
            is_silent=is_silent,
            metadata=metadata,
        )
        progress("系统声音录音已保存", 1.0)
        self._cleanup_stop_file()
        return RecordingResult(
            source=RecordingSource.SYSTEM_AUDIO,
            output_path=output_path,
            tracks=(track,),
            duration_seconds=duration,
            sample_rate=48000,
            channels=2,
            metadata=metadata,
        )

    def _cleanup_output(self) -> None:
        for path in (self._request.output_path, self._request.output_path.with_suffix(".analysis.wav")):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def _cleanup_stop_file(self) -> None:
        try:
            if self._stop_file.exists():
                self._stop_file.unlink()
        except OSError:
            pass

    def _drain_stdout(self):
        while True:
            try:
                yield self._stdout_queue.get_nowait()
            except queue.Empty:
                break


class MacOSSystemAudioBackend:
    backend_id = "macos_screencapturekit"
    display_name = "系统声音"
    platforms = ("darwin",)

    def supports(self, source: RecordingSource) -> bool:
        return source == RecordingSource.SYSTEM_AUDIO

    def start(
        self,
        request: RecordingRequest,
        progress: ProgressCallback | None = None,
    ) -> RecordingSession:
        progress = progress or _noop
        if not self.supports(request.source):
            raise RecordingBackendError("当前录音来源暂不支持")
        helper = ensure_macos_system_audio_helper(progress)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        stop_file = request.output_path.with_suffix(".stop")
        try:
            if stop_file.exists():
                stop_file.unlink()
        except OSError:
            pass
        max_seconds = request.max_duration_seconds or 7200
        process = subprocess.Popen(
            [str(helper), str(request.output_path), str(stop_file), str(max_seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout_queue: "queue.Queue[str]" = queue.Queue()
        stderr_queue: "queue.Queue[str]" = queue.Queue()
        readers = (
            threading.Thread(target=_reader_thread, args=(process.stdout, stdout_queue), daemon=True),
            threading.Thread(target=_reader_thread, args=(process.stderr, stderr_queue), daemon=True),
        )
        for reader in readers:
            reader.start()
        session = MacOSSystemAudioSession(request, process, stop_file, stdout_queue, stderr_queue, readers)

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                for reader in readers:
                    reader.join(timeout=0.5)
                session.cancel()
                raise _capture_failure(stderr_queue, "启动")
            try:
                line = stdout_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if line == "started":
                progress("正在录制电脑播放的声音", -1)
                return session
            session._observe_timing(line)
        session.cancel()
        raise RecordingBackendError("系统声音录制启动超时，已停止本次尝试。请重试或重新打开 Lulu。")


def is_macos_system_audio_available() -> bool:
    if platform.system() != "Darwin":
        return False
    bundled = _bundled_helper()
    if bundled is not None and bundled.is_file() and os.access(bundled, os.X_OK):
        return True
    return not getattr(sys, "frozen", False) and (os.access(_helper_paths()[1], os.X_OK) or shutil.which("swiftc") is not None)


__all__ = [
    "MacOSSystemAudioBackend",
    "MacOSSystemAudioSession",
    "RecordingBackendError",
    "ensure_macos_system_audio_helper",
    "is_macos_system_audio_available",
]
