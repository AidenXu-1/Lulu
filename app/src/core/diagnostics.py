"""本机有界故障记录：只记阶段、错误码和代码位置，不记请求或秘密。"""
import json
from pathlib import Path
import re
from threading import RLock
from datetime import datetime, timezone
import traceback

_lock = RLock()
_stack_file = None
_CODE = re.compile(r"(?:CORE|GUI|JOB|KEYCHAIN|PRODUCT|COMPONENT|AGENT|CONFIG|INSTALLATION|DOUYIN|LOCAL_INTEGRITY)_[A-Z0-9_]{1,100}")


def failure_code(exc):
    for value in (getattr(exc, "code", None), getattr(exc, "reason_code", None), str(exc)):
        if isinstance(value, str) and _CODE.fullmatch(value):
            return value
    native = getattr(exc, "native_code", None)
    return f"CORE_NATIVE_{native}" if type(native) is int else "CORE_OPERATION_FAILED"


def enable_core_stack_diagnostics(root):
    """SIGUSR1 writes only Python frames to a private, process-specific file."""
    global _stack_file
    if _stack_file is not None:
        return
    import faulthandler
    import os
    import signal
    import stat
    descriptor = None
    try:
        directory = Path(root) / "diagnostics"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            return
        descriptor = os.open(directory / f"core-stack-{os.getpid()}.txt",
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(descriptor, "wb", buffering=0)
        descriptor = None
        try:
            faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            stream.close()
            return
        _stack_file = stream
    except OSError:
        if descriptor is not None:
            os.close(descriptor)


def record_failure(stage, exc, *, root=None):
    try:
        from .models_manager import app_data_dir
        from .updater.secure_io import atomic_json, read_nofollow
        path = (Path(root) if root else app_data_dir()) / "diagnostics" / "failures.json"
        causes = []
        current = exc
        for _ in range(6):
            if current is None:
                break
            causes.append({"type":type(current).__name__, "code":failure_code(current),
                "frames":[{"file":Path(frame.filename).name, "line":frame.lineno,
                           "function":frame.name} for frame in traceback.extract_tb(current.__traceback__)[-6:]]})
            if isinstance(current, OSError) and type(current.errno) is int:
                causes[-1]['errno'] = current.errno
            item_kind = getattr(current, 'lulu_storage_item_kind', None)
            if item_kind in ('core_registry', 'core_key', 'core_anchor'):
                causes[-1]['storage_item_kind'] = item_kind
            native = getattr(current, "native_code", None)
            if type(native) is int and native in {2, 10, 11, 12, 20, 21, 22, 30}:
                causes[-1]["native_code"] = native
            current = current.__cause__
        event = {"at":datetime.now(timezone.utc).isoformat(), "stage":stage, "causes":causes}
        with _lock:
            try:
                previous = json.loads(read_nofollow(path, mode=0o600, maximum=512*1024))["events"]
                if not isinstance(previous, list):
                    previous = []
            except (OSError, ValueError, KeyError):
                previous = []
            atomic_json(path, {"schema":1,"events":(previous+[event])[-100:]})
    except Exception:
        # 记录器不能改变主操作的错误，磁盘不可用时仍传递原始失败。
        return


class TranscriptionAttemptTrace:
    """A bounded local timeline containing only identifiers and fixed enums."""
    STAGES = frozenset({"starting", "waiting_media", "reuse_check", "subtitles", "download",
                        "conversion", "transcription", "result", "local_service", "saving"})

    def __init__(self, job_id, *, root=None):
        import hashlib
        import time
        import uuid
        self.job = hashlib.sha256(str(job_id).encode()).hexdigest()[:24]
        self.attempt = uuid.uuid4().hex
        self.root = root
        self.started = time.monotonic()
        self.stage = "starting"
        self.enter(self.stage)

    def _record(self, outcome, exc=None):
        import time
        try:
            from .models_manager import app_data_dir
            from .updater.secure_io import atomic_json, read_nofollow
            path = (Path(self.root) if self.root else app_data_dir()) / "diagnostics" / "transcription-attempts.json"
            event = {"at": datetime.now(timezone.utc).isoformat(), "job": self.job,
                     "attempt": self.attempt, "stage": self.stage, "outcome": outcome,
                     "elapsed_ms": int((time.monotonic() - self.started) * 1000)}
            if exc is not None:
                event["error_code"] = failure_code(exc)
                event["error_type"] = next((name for cls, name in (
                    (TimeoutError, "timeout"), (InterruptedError, "cancelled"),
                    (OSError, "io"), (ValueError, "invalid_input")) if isinstance(exc, cls)), "operation")
            with _lock:
                try:
                    previous = json.loads(read_nofollow(path, mode=0o600, maximum=512*1024))["events"]
                    if not isinstance(previous, list):
                        previous = []
                except (OSError, ValueError, KeyError):
                    previous = []
                atomic_json(path, {"schema": 1, "events": (previous + [event])[-300:]})
        except Exception:
            return

    def enter(self, stage):
        if stage not in self.STAGES:
            return
        self.stage = stage
        self._record("entered")

    def returned(self, outcome, exc=None):
        if outcome in {"succeeded", "cancelled", "failed", "saved", "save_failed", "thread_finished"}:
            self._record(outcome, exc)
