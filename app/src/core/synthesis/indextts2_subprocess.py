"""IndexTTS2 subprocess adapter prototype.

The main Lulu process never imports IndexTTS2. It writes a JSON request, starts
a stdlib-only worker process, and consumes JSONL events from stdout.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from ..audio.output_level import level_pcm16_wav
from .base import SynthesisEngine, SynthesisError, SynthesisRequest, SynthesisResult, _noop
from .errors import scrub_detail
from .indextts2_presets import emotion_vector_for, emotion_weight_for, normalize_emotion_intensity, normalize_emotion_vector
from .indextts2_readiness import IndexTTS2RuntimeStatus, check_runtime
from .stitcher import stitch_wavs


ENGINE_ID = "local_indextts2"


def detect_device() -> str:
    # Supported macOS builds already require an MPS-capable OS. Loading another
    # Torch in the GUI process only to choose this worker's device adds a long
    # cold start and memory that cannot be released with the TTS background.
    if sys.platform == 'darwin':
        return 'mps' if platform.machine() == 'arm64' else 'cpu'
    from ..audio.device import detect_device as detect_other_device
    return detect_other_device()


def _worker_error(event: dict[str, Any]) -> SynthesisError:
    detail = scrub_detail(str(event.get("detail_redacted") or ""))
    message = str(event.get("message") or "IndexTTS 2.5 生成失败。")
    error = SynthesisError(f"{message}:{detail}" if detail else message)
    if 'out of memory' in detail.lower():
        error.reason_code = 'INDEXTTS_MEMORY_LIMIT'
        return error
    code = event.get("code")
    if code in {"RUNTIME_UNAVAILABLE", "GENERATION_FAILED", "STITCH_FAILED", "INCOMPLETE_AUDIO", "INVALID_REQUEST"}:
        error.reason_code = f"INDEXTTS_{code}"
    return error


def _worker_script() -> Path:
    name = "indextts2_worker.py"
    direct = Path(__file__).with_name(name)
    if direct.exists():
        return direct
    exe = Path(sys.executable).resolve()
    candidates = [
        exe.parent.parent / "Resources" / "src" / "core" / "synthesis" / name,
        exe.parent.parent / "Frameworks" / "src" / "core" / "synthesis" / name,
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(str(meipass)).resolve()
        candidates.extend(
            [
                root / "src" / "core" / "synthesis" / name,
                root.parent / "Resources" / "src" / "core" / "synthesis" / name,
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return direct


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        proc.wait(timeout=5)


def _start_worker(request_json: Path, runtime_python: Path | None = None) -> subprocess.Popen:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    worker_script = _worker_script()
    if runtime_python is not None:
        cmd = [str(runtime_python), "-I", "-s", str(worker_script), "--request-json", str(request_json)]
    else:
        cmd = [sys.executable, str(worker_script), "--request-json", str(request_json)]
    return subprocess.Popen(cmd, **kwargs)


def _start_persistent_worker(runtime_python: Path | None = None) -> subprocess.Popen:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    worker_script = _worker_script()
    if runtime_python is not None:
        cmd = [str(runtime_python), "-I", "-s", str(worker_script), "--server"]
    else:
        cmd = [sys.executable, str(worker_script), "--server"]
    return subprocess.Popen(cmd, **kwargs)


def _parse_event(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _read_stream(stream, out: "queue.Queue[str | None]") -> None:
    try:
        for line in iter(stream.readline, ""):
            out.put(line)
    finally:
        out.put(None)


class PersistentWorkerCrashed(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode

    @property
    def was_forced_to_exit(self) -> bool:
        return self.returncode is not None and self.returncode < 0


class PersistentWorkerBusy(RuntimeError):
    pass


def _timeout_seconds_from_payload(payload: dict[str, Any]) -> float:
    try:
        timeout = float((payload.get("options") or {}).get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    return max(0.0, timeout)


def _timeout_error() -> SynthesisError:
    return SynthesisError("IndexTTS2 生成超时，请重试；如果连续出现，请释放后台后再生成。")


def _mac_process_memory(pid: int) -> tuple[int, int]:
    """Read RSS and physical footprint, which includes compressed memory (Darwin V0)."""
    import ctypes
    class Usage(ctypes.Structure):
        _fields_ = [('uuid', ctypes.c_ubyte * 16)] + [(name, ctypes.c_uint64) for name in (
            'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins',
            'wired_size', 'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime')]
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_pid_rusage.restype = ctypes.c_int
    usage = Usage()
    if lib.proc_pid_rusage(pid, 0, ctypes.byref(usage)) != 0:
        raise OSError(ctypes.get_errno(), 'worker memory measurement unavailable')
    return int(usage.resident_size), int(usage.phys_footprint)


class _ProcessMemoryMonitor:
    """Observe model memory, without an app-imposed quota.

    macOS and the inference runtime retain their normal allocation safeguards.
    Missing telemetry must not discard an otherwise usable generation.
    """
    def __init__(self, proc, device: str, *, model_version: str = '2.0'):
        self.proc = proc
        self.enabled = sys.platform == 'darwin'
        self.next_check = 0.0
        self.peak_rss = 0
        self.peak_footprint = 0
        self.measured = False

    def check(self):
        if not self.enabled or time.monotonic() < self.next_check or self.proc.poll() is not None:
            return
        self.next_check = time.monotonic() + 0.25
        try:
            rss, footprint = _mac_process_memory(self.proc.pid)
        except OSError:
            return
        self.measured = True
        self.peak_rss = max(self.peak_rss, rss)
        self.peak_footprint = max(self.peak_footprint, footprint)

    def annotate(self, result):
        if self.enabled:
            result.setdefault('meta', {}).update(memory_measurement_available=self.measured,
                memory_policy='observe_only', memory_peak_rss_bytes=self.peak_rss,
                memory_peak_footprint_bytes=self.peak_footprint)
        return result


class IndexTTS2PersistentWorkerManager:
    def __init__(self, idle_timeout_seconds: int = 600) -> None:
        self.idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stdout_queue: "queue.Queue[str | None]" = queue.Queue()
        self._stderr_queue: "queue.Queue[str | None]" = queue.Queue()
        self._stdout_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._runtime_python: Path | None = None
        self._model_version: str | None = None
        self._last_used = 0.0
        self._load_count = 0
        self._idle_thread = threading.Thread(target=self._idle_watch_loop, daemon=True)
        self._idle_thread.start()

    def _idle_watch_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.reap_idle()
            except Exception:
                pass

    def status(self) -> dict[str, Any]:
        alive = self._proc is not None and self._proc.poll() is None
        idle_for = max(0.0, time.monotonic() - self._last_used) if self._last_used else 0.0
        return {
            "alive": alive,
            "busy": self._lock.locked(),
            "pid": self._proc.pid if alive and self._proc is not None else None,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "idle_for_seconds": round(idle_for, 1),
            "load_count": self._load_count,
            "model_version": self._model_version if alive else None,
        }

    def _drop_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        for reader in (self._stdout_reader, self._stderr_reader):
            if reader is not None:
                reader.join(timeout=1)
        self._proc = None
        self._stdout_reader = None
        self._stderr_reader = None
        self._stdout_queue = queue.Queue()
        self._stderr_queue = queue.Queue()

    def shutdown(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(json.dumps({"command": "shutdown", "job_id": "shutdown"}) + "\n")
                    proc.stdin.flush()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            self._drop_process()

    def cancel_current(self) -> None:
        with self._lock:
            self._drop_process()

    def reap_idle(self) -> bool:
        if self.idle_timeout_seconds <= 0:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._proc is None or self._proc.poll() is not None or not self._last_used:
                return False
            if time.monotonic() - self._last_used < self.idle_timeout_seconds:
                return False
            self._drop_process()
            return True
        finally:
            self._lock.release()

    def _ensure_process(self, status: IndexTTS2RuntimeStatus) -> None:
        runtime_python = Path(getattr(status, "python_executable", status.runtime_dir / "python" / "bin" / "python3"))
        if self._proc is not None and self._proc.poll() is None and self._runtime_python == runtime_python:
            return
        self._drop_process()
        self._runtime_python = runtime_python
        self._proc = _start_persistent_worker(runtime_python)
        self._model_version = getattr(status, "model_version", None)
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._stdout_reader = threading.Thread(target=_read_stream, args=(self._proc.stdout, self._stdout_queue), daemon=True)
        self._stderr_reader = threading.Thread(target=_read_stream, args=(self._proc.stderr, self._stderr_queue), daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

    def generate(
        self,
        payload: dict[str, Any],
        status: IndexTTS2RuntimeStatus,
        progress,
        is_cancelled,
        *,
        output_wav: Path,
        base_fraction: float = 0.0,
        span_fraction: float = 1.0,
    ) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise PersistentWorkerBusy("IndexTTS2 后台正忙，请等待当前生成完成或先取消。")
        try:
            if is_cancelled():
                raise _Cancelled()
            self._ensure_process(status)
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise PersistentWorkerCrashed("IndexTTS2 后台启动失败。")
            try:
                memory_guard = _ProcessMemoryMonitor(proc, str(payload.get('device', '')),
                                               model_version=str(payload.get('model_version', '2.0')))
            except Exception:
                self._drop_process()
                raise
            command = dict(payload)
            command["command"] = "generate"
            proc.stdin.write(json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n")
            proc.stdin.flush()
            temp_wav = Path(str(payload.get("output", {}).get("temp_wav_path") or ""))
            result_event: dict[str, Any] | None = None
            stderr_lines: list[str] = []
            timeout = _timeout_seconds_from_payload(payload)
            deadline = time.monotonic() + timeout if timeout > 0 else None
            while True:
                if is_cancelled():
                    self._drop_process()
                    raise _Cancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    self._drop_process()
                    raise _timeout_error()
                try:
                    memory_guard.check()
                except SynthesisError:
                    self._drop_process()
                    raise
                self.reap_idle()
                if proc.poll() is not None:
                    raise PersistentWorkerCrashed(
                        "IndexTTS2 后台进程已退出。",
                        returncode=proc.returncode,
                    )
                while True:
                    try:
                        err_line = self._stderr_queue.get_nowait()
                    except queue.Empty:
                        break
                    if err_line is not None:
                        stderr_lines.append(err_line)
                try:
                    line = self._stdout_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    returncode = proc.poll()
                    if returncode is None:
                        try:
                            returncode = proc.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            returncode = None
                    raise PersistentWorkerCrashed(
                        "IndexTTS2 后台输出已关闭。",
                        returncode=returncode,
                    )
                event = _parse_event(line.strip())
                if event is None:
                    continue
                event_job_id = str(event.get("job_id") or "")
                if event_job_id and event_job_id != str(payload.get("job_id") or ""):
                    continue
                event_type = event.get("type")
                if event_type == "progress":
                    inner = float(event.get("fraction", -1.0))
                    mapped = -1.0 if inner < 0 else base_fraction + inner * span_fraction
                    progress(str(event.get("message") or "生成中"), mapped)
                elif event_type == "lifecycle":
                    if event.get("state") == "ready":
                        try:
                            self._load_count = max(self._load_count, int(event.get("load_count") or 0))
                        except (TypeError, ValueError):
                            pass
                    progress(str(event.get("message") or "IndexTTS2 后台运行中"), -1.0)
                elif event_type == "result":
                    result_event = event
                elif event_type == "error":
                    error = _worker_error(event)
                    if getattr(error, 'reason_code', '') == 'INDEXTTS_MEMORY_LIMIT':
                        self._drop_process()
                    raise error
                if result_event is not None:
                    break
            if result_event is None or not temp_wav.exists():
                detail = scrub_detail("\n".join(stderr_lines))
                raise SynthesisError(f"IndexTTS2 未返回有效合成结果。{':' + detail if detail else ''}")
            shutil.move(str(temp_wav), str(output_wav))
            self._last_used = time.monotonic()
            result_event["_payload"] = payload
            result_event["_persistent_status"] = self.status()
            return memory_guard.annotate(result_event)
        finally:
            self._lock.release()


_PERSISTENT_MANAGER = IndexTTS2PersistentWorkerManager()


def persistent_worker_status() -> dict[str, Any]:
    _PERSISTENT_MANAGER.reap_idle()
    return _PERSISTENT_MANAGER.status()


def shutdown_persistent_worker() -> None:
    _PERSISTENT_MANAGER.shutdown()


def cancel_persistent_worker() -> None:
    _PERSISTENT_MANAGER.cancel_current()


def _build_payload(
    request: SynthesisRequest,
    status: IndexTTS2RuntimeStatus,
    temp_wav: Path,
    job_id: str,
) -> dict[str, Any]:
    options = dict(request.engine_options or {})
    preset = str(options.get("emotion_preset") or "natural")
    intensity = normalize_emotion_intensity(str(options.get("emotion_intensity") or "standard"), preset)
    if options.get("emotion_vector") is not None:
        vector = list(normalize_emotion_vector(options.get("emotion_vector")))
        mode = "custom_vector"
    else:
        vector = list(emotion_vector_for(preset))
        mode = "preset_vector"
    weight = float(options.get("emotion_weight", emotion_weight_for(intensity, preset)))
    # 情感控制方式(五选一):follow 跟随音色 / emoaudio 情感参考音频 / template 情感模板
    # / vector 8维向量 / emotext 情感描述文本。未显式给时按是否有自定义向量回退(向后兼容)。
    control_mode = str(options.get("emotion_mode") or "").strip().lower()
    if control_mode not in {"follow", "emoaudio", "template", "vector", "emotext"}:
        control_mode = "vector" if options.get("emotion_vector") is not None else "template"
    emotion_audio_path = str(options.get("emotion_audio_path") or "")
    emotion_text = str(options.get("emotion_text") or "")
    runtime_python = getattr(status, "python_executable", status.runtime_dir / "python" / "bin" / "python3")
    vendor_dir = getattr(status, "vendor_dir", status.runtime_dir / "vendor" / "index-tts")
    nltk_data_dir = getattr(status, "nltk_data_dir", status.runtime_dir / "python" / "nltk_data")
    model_version = str(getattr(status, "model_version", "2.0"))
    requested_device = str(options.get("device") or detect_device())
    # Match the GUI for both versions. The old 2.0 MPS route silently reduced
    # sentence context/search and could fail at its private 8 GiB quota.
    # Keep full reference/defaults on CPU until full-parameter MPS is validated.
    device = 'cpu' if requested_device == 'mps' else requested_device
    speech = {"language": str(options.get("language") or "ZH").upper(),
              "speed": float(options.get("speed", 1.0)),
              "random_emotion": bool(options.get("random_emotion", False))}
    if model_version in ('2.0', '2.5') and 'generation_options' in options:
        from .indextts2_worker import validate_index_generation
        try:
            generation = validate_index_generation(options['generation_options'], model_version)
            if generation:
                speech['generation'] = generation
        except ValueError as exc:
            raise SynthesisError(str(exc)) from exc
    if model_version == '2.5' and 'do_sample' in options:
        if not isinstance(options['do_sample'], bool):
            raise SynthesisError('生成采样开关必须为开启或关闭。')
        speech['do_sample'] = options['do_sample']
    return {
        "schema_version": 1,
        "job_id": job_id,
        "engine": ENGINE_ID,
        "model_version": model_version,
        "speech": speech,
        "device": device,
        "requested_device": requested_device,
        "runtime_dir": str(status.runtime_dir),
        "runtime_python": str(runtime_python),
        "vendor_dir": str(vendor_dir),
        "nltk_data_dir": str(nltk_data_dir),
        "model_dir": str(status.model_dir),
        "text": request.text,
        "text_segments": [str(seg).strip() for seg in options.get("text_segments") or [] if str(seg).strip()],
        "voice": {
            "id": request.voice.id,
            "reference_audio_path": request.voice.reference_audio_path,
            "reference_text": request.voice.reference_text,
        },
        "emotion": {
            "preset": preset,
            "intensity": intensity,
            "vector": vector,
            "weight": weight,
            "mode": mode,
            "control_mode": control_mode,
            "audio_path": emotion_audio_path,
            "text": emotion_text,
            "summary": str(options.get("emotion_summary") or ""),
            "source_preset": str(options.get("emotion_source_preset") or preset),
            "source_intensity": str(options.get("emotion_source_intensity") or intensity),
        },
        "output": {
            "temp_wav_path": str(temp_wav),
            "final_wav_path": str(request.output_path.with_suffix(".wav")),
            "format": "wav",
        },
        "readiness": status.snapshot(),
        "options": {
            "timeout_seconds": float(options.get("timeout_seconds", 1800)),
            "normalize_audio": bool(options.get("normalize_audio", True)),
            "max_text_chars_per_segment": int(options.get("max_text_chars_per_segment", 300)),
        },
        "style_preset": dict(options.get("style_preset") or {}),
    }


class IndexTTS2SubprocessEngine(SynthesisEngine):
    name = ENGINE_ID
    display_name = "本地 · IndexTTS2 情感配音"
    requires_network = False

    def _synthesize_persistent(
        self,
        request: SynthesisRequest,
        status: IndexTTS2RuntimeStatus,
        output_wav: Path,
        job_id: str,
        progress,
        is_cancelled,
        *,
        base_fraction: float = 0.0,
        span_fraction: float = 1.0,
    ) -> dict[str, Any]:
        temp_root = Path(tempfile.gettempdir()) / "lulu_indextts2_persistent"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="job_", dir=str(temp_root)) as tmp:
            tmp_dir = Path(tmp)
            temp_wav = tmp_dir / "output.wav"
            payload = _build_payload(request, status, temp_wav, job_id)
            return _PERSISTENT_MANAGER.generate(
                payload,
                status,
                progress,
                is_cancelled,
                output_wav=output_wav,
                base_fraction=base_fraction,
                span_fraction=span_fraction,
            )

    def _synthesize_once(
        self,
        request: SynthesisRequest,
        status: IndexTTS2RuntimeStatus,
        output_wav: Path,
        job_id: str,
        progress,
        is_cancelled,
        *,
        base_fraction: float = 0.0,
        span_fraction: float = 1.0,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="lulu_indextts2_") as tmp:
            tmp_dir = Path(tmp)
            temp_wav = tmp_dir / "output.wav"
            payload = _build_payload(request, status, temp_wav, job_id)
            request_json = tmp_dir / "request.json"
            request_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            runtime_python = getattr(status, "python_executable", None)
            proc = _start_worker(request_json, Path(runtime_python) if runtime_python else None)
            timeout = _timeout_seconds_from_payload(payload)
            deadline = time.monotonic() + timeout if timeout > 0 else None
            result_event: dict[str, Any] | None = None
            stderr_lines: list[str] = []
            stdout_done = False
            stderr_done = False
            stdout_queue: "queue.Queue[str | None]" = queue.Queue()
            stderr_queue: "queue.Queue[str | None]" = queue.Queue()
            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout_reader = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_queue), daemon=True)
            stderr_reader = threading.Thread(target=_read_stream, args=(proc.stderr, stderr_queue), daemon=True)
            stdout_reader.start()
            stderr_reader.start()
            try:
                memory_guard = _ProcessMemoryMonitor(proc, str(payload.get('device', '')),
                                               model_version=str(payload.get('model_version', '2.0')))
                while True:
                    memory_guard.check()
                    if is_cancelled():
                        _terminate_process(proc)
                        raise _Cancelled()
                    if deadline is not None and time.monotonic() >= deadline:
                        _terminate_process(proc)
                        raise _timeout_error()

                    while True:
                        try:
                            line = stdout_queue.get_nowait()
                        except queue.Empty:
                            break
                        if line is None:
                            stdout_done = True
                            break
                        event = _parse_event(line.strip())
                        if event is None:
                            continue
                        event_type = event.get("type")
                        if event_type == "progress":
                            inner = float(event.get("fraction", -1.0))
                            mapped = -1.0 if inner < 0 else base_fraction + inner * span_fraction
                            progress(str(event.get("message") or "生成中"), mapped)
                        elif event_type == "result":
                            result_event = event
                        elif event_type == "error":
                            raise _worker_error(event)

                    while True:
                        try:
                            err_line = stderr_queue.get_nowait()
                        except queue.Empty:
                            break
                        if err_line is None:
                            stderr_done = True
                            break
                        stderr_lines.append(err_line)

                    if proc.poll() is not None and stdout_done and stderr_done:
                        break
                    time.sleep(0.05)
            finally:
                if proc.poll() is None:
                    _terminate_process(proc)
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                while True:
                    try:
                        line = stdout_queue.get_nowait()
                    except queue.Empty:
                        break
                    if line is not None:
                        event = _parse_event(line.strip())
                        if event is not None and event.get("type") == "result":
                            result_event = event
                while True:
                    try:
                        err_line = stderr_queue.get_nowait()
                    except queue.Empty:
                        break
                    if err_line is not None:
                        stderr_lines.append(err_line)

            if proc.returncode != 0:
                detail = scrub_detail("\n".join(stderr_lines))
                raise SynthesisError(f"IndexTTS2 生成失败:{detail}")
            if result_event is None or not temp_wav.exists():
                raise SynthesisError("IndexTTS2 未返回有效合成结果。")
            shutil.move(str(temp_wav), str(output_wav))
            result_event["_payload"] = payload
            return memory_guard.annotate(result_event)

    def synthesize(self, request: SynthesisRequest, progress=None, is_cancelled=None) -> SynthesisResult:
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        text = (request.text or "").strip()
        if not text:
            raise SynthesisError("请输入要合成的文本。")
        ref = Path(request.voice.reference_audio_path)
        if not ref.exists():
            raise SynthesisError("参考音频不可用,请重新选择。")

        version = (request.engine_options or {}).get("model_version")
        status = check_runtime(expected_version=version) if version is not None else check_runtime()
        if version is not None and status.model_version != version:
            raise SynthesisError("所选 IndexTTS 版本与本地模型不一致，已停止生成。")
        if not status.ready:
            raise SynthesisError(f"IndexTTS2 本地环境尚未准备完成:{status.detail_redacted}")

        # This engine is also called directly by its UI worker, outside pipeline.py.
        from .qwen3_subprocess import shutdown_persistent_worker as release_qwen
        if not release_qwen():
            raise SynthesisError("Qwen 正在生成，请先等待或取消。")

        started = time.perf_counter()
        job_id = f"indextts2-{uuid.uuid4().hex[:12]}"
        output_wav = request.output_path.with_suffix(".wav")
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        options = dict(request.engine_options or {})
        text_segments = [str(seg).strip() for seg in options.get("text_segments") or [] if str(seg).strip()]
        segment_count = len(text_segments)
        result_event: dict[str, Any]
        payload: dict[str, Any]
        use_persistent = bool(options.get("use_persistent_worker", type(self) is IndexTTS2SubprocessEngine))
        generation_span = 0.94 if getattr(status, "model_version", "2.0") in ("2.0", "2.5") else 1.0
        if use_persistent:
            try:
                result_event = self._synthesize_persistent(request, status, output_wav, job_id, progress, is_cancelled,
                                                           span_fraction=generation_span)
            except (_Cancelled, PersistentWorkerBusy):
                raise
            except SynthesisError:
                raise
            except PersistentWorkerCrashed as exc:
                if exc.was_forced_to_exit:
                    raise SynthesisError(
                        f"IndexTTS2 worker forced_exit returncode={exc.returncode}"
                    ) from exc
                progress("IndexTTS2 后台异常，改用一次性生成恢复", -1.0)
                result_event = self._synthesize_once(request, status, output_wav, job_id, progress, is_cancelled,
                                                     span_fraction=generation_span)
            payload = dict(result_event.get("_payload") or _build_payload(request, status, output_wav, job_id))
        else:
            try:
                result_event = self._synthesize_once(request, status, output_wav, job_id, progress, is_cancelled,
                                                     span_fraction=generation_span)
            except _Cancelled:
                raise
            except SynthesisError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SynthesisError(scrub_detail(str(exc))) from exc
            payload = dict(result_event.get("_payload") or _build_payload(request, status, output_wav, job_id))

        output_level = None
        if payload.get("model_version") in ("2.0", "2.5"):
            def check_output_cancelled():
                if is_cancelled():
                    raise _Cancelled()
            progress("调整输出音量…", 0.95)
            try:
                output_level = level_pcm16_wav(output_wav, check_cancelled=check_output_cancelled)
            except _Cancelled:
                raise
            except (OSError, ValueError, EOFError, wave.Error) as exc:
                raise SynthesisError("输出音量调整失败，原始音频已保留，本次输出未完成。") from exc

        duration = float(result_event.get("duration_seconds") or 0.0)
        output_path = output_wav
        if request.output_format == "mp3":
            from ..audio.converter import wav_to_mp3
            progress("导出 MP3…", 0.96)
            output_path = request.output_path.with_suffix(".mp3")
            wav_to_mp3(output_wav, output_path)
        if output_level is not None or request.output_format == "mp3":
            progress("合成完成", 1.0)
        result_meta = dict(result_event.get("meta") or {})
        if output_level is not None:
            result_meta["output_level"] = output_level
        if segment_count > 1:
            result_meta.setdefault("segment_generation", True)
            result_meta.setdefault("segment_count", segment_count)
            result_meta.setdefault("segment_lengths", [len(seg) for seg in text_segments])
        return SynthesisResult(
            output_path=output_path,
            wav_path=output_wav,
            duration_seconds=duration,
            engine=self.name,
            voice_id=request.voice.id,
            text=text,
            meta={
                "job_id": job_id,
                "runtime_dir": scrub_detail(str(status.runtime_dir)),
                "model_dir": scrub_detail(str(status.model_dir)),
                "readiness": status.snapshot(),
                "model_version": payload.get("model_version", "2.0"),
                "requested_device": payload.get("requested_device", payload.get("device")),
                "execution_policy": ('full_reference_cpu' if payload.get('model_version') in ('2.0', '2.5')
                                     and payload.get('device') == 'cpu' else 'device_default'),
                "speech": dict(payload.get("speech") or {}),
                "preview": bool(options.get("preview", False)),
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "persistent_worker": dict(result_event.get("_persistent_status") or {}),
                "style_preset": dict((request.engine_options or {}).get("style_preset") or {}),
                "text_length": dict((request.engine_options or {}).get("text_length") or {}),
                **result_meta,
                "emotion_preset": str(payload.get("emotion", {}).get("preset") or "natural"),
                "emotion_intensity": str(payload.get("emotion", {}).get("intensity") or "standard"),
                "emotion_weight": float(payload.get("emotion", {}).get("weight", 0.8)),
                "emotion_vector": list(payload.get("emotion", {}).get("vector") or []),
                "emotion_mode": str(payload.get("emotion", {}).get("mode") or "preset_vector"),
                "emotion_control_mode": str(payload.get("emotion", {}).get("control_mode") or ""),
                "emotion_text": str(payload.get("emotion", {}).get("text") or ""),
                "emotion_summary": str(payload.get("emotion", {}).get("summary") or ""),
                "emotion_source_preset": str(payload.get("emotion", {}).get("source_preset") or ""),
                "emotion_source_intensity": str(payload.get("emotion", {}).get("source_intensity") or ""),
            },
        )


class _Cancelled(Exception):
    pass


def was_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, _Cancelled)
