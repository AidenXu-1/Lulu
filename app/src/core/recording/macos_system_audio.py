"""macOS ScreenCaptureKit system-audio recording backend."""

from __future__ import annotations

import hashlib
import math
import os
import platform
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

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
            writer.startWriting()
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
            started = true
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
            fputs("capture_error: \(error.localizedDescription)\n", stderr)
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


def ensure_macos_system_audio_helper(progress: ProgressCallback | None = None) -> Path:
    progress = progress or _noop
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RecordingBackendError("系统声音录制组件不可用")

    source_path, binary_path, module_cache = _helper_paths()
    if binary_path.exists():
        return binary_path

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


def _wav_stats(path: Path) -> dict[str, float]:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        data = wav.readframes(frames)
    if not data:
        return {"sample_rate": float(sample_rate), "duration": 0.0, "rms": 0.0, "peak": 0.0}
    samples = struct.unpack("<" + "h" * (len(data) // 2), data)
    if not samples:
        return {"sample_rate": float(sample_rate), "duration": 0.0, "rms": 0.0, "peak": 0.0}
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    peak = max(abs(v) for v in samples)
    return {
        "sample_rate": float(sample_rate),
        "duration": float(frames) / float(sample_rate or 1),
        "rms": float(rms),
        "peak": float(peak),
    }


class MacOSSystemAudioSession:
    def __init__(
        self,
        request: RecordingRequest,
        process: subprocess.Popen,
        stop_file: Path,
        stdout_queue: "queue.Queue[str]",
    ):
        self._request = request
        self._process = process
        self._stop_file = stop_file
        self._stdout_queue = stdout_queue
        self._cancelled = False

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
            self._cleanup_stop_file()
            self._cleanup_output()
            raise RecordingBackendError("系统声音录制失败,请检查系统声音权限后重试")

        output_path = self._request.output_path
        if not output_path.exists() or output_path.stat().st_size == 0:
            self._cleanup_stop_file()
            self._cleanup_output()
            raise RecordingBackendError("没有生成可用的系统声音录音")

        metadata: dict[str, object] = {"stdout": list(self._drain_stdout())}
        wav_path = output_path.with_suffix(".analysis.wav")
        is_silent: bool | None = None
        duration: float | None = None
        if _decode_to_wav(output_path, wav_path):
            stats = _wav_stats(wav_path)
            metadata["analysis"] = stats
            duration = stats.get("duration")
            is_silent = stats.get("rms", 0.0) < 80.0 or stats.get("peak", 0.0) < 200.0
            try:
                wav_path.unlink()
            except OSError:
                pass
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
        threading.Thread(target=_reader_thread, args=(process.stdout, stdout_queue), daemon=True).start()
        threading.Thread(target=_reader_thread, args=(process.stderr, stderr_queue), daemon=True).start()

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RecordingBackendError("系统声音录制启动失败,请检查系统声音权限")
            try:
                line = stdout_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if line == "started":
                progress("正在录制电脑播放的声音", -1)
                return MacOSSystemAudioSession(request, process, stop_file, stdout_queue)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        raise RecordingBackendError("系统声音录制启动超时,请检查系统声音权限")


def is_macos_system_audio_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("swiftc") is not None


__all__ = [
    "MacOSSystemAudioBackend",
    "MacOSSystemAudioSession",
    "RecordingBackendError",
    "ensure_macos_system_audio_helper",
    "is_macos_system_audio_available",
]
