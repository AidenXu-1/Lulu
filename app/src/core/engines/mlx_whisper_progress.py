"""Whisper MLX 音频位置进度桥。

上游 ``mlx_whisper.transcribe`` 已按解码窗口维护真实帧进度，但只把它交给
命令行 ``tqdm``。本模块在一次调用期间桥接这个现成状态，不复制、不切分、
不重跑推理；进度合同不兼容时保留取消边界，无法安全取消时拒绝推理。
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from types import ModuleType
from typing import Any

AudioPositionCallback = Callable[[float], None]

_BRIDGE_LOCK = threading.RLock()


class _ProgressController:
    def __init__(
        self,
        callback: AudioPositionCallback,
        *,
        window_frames: int,
    ) -> None:
        self._callback = callback
        self._window_frames = max(1, int(window_frames))
        self._total = 0
        self._advanced = 0
        self._current_segment: object | None = None
        self._current_reported = True
        self._last_percent = -1
        self._finished = False

    def start(self, total: object) -> None:
        try:
            clean_total = max(0, int(total))
        except (TypeError, ValueError, OverflowError):
            clean_total = 0
        self._total = clean_total
        self._advanced = 0
        self._current_segment = None
        self._current_reported = True
        self._last_percent = -1
        self._finished = False
        if self._total:
            self._emit()

    def before_decode(self, segment: object) -> None:
        if not self._total or self._finished:
            return
        if self._current_segment is None:
            self._current_segment = segment
            self._current_reported = False
            return
        if segment is self._current_segment:
            # 温度回退会对同一个窗口重复 decode，不能重复推进。
            return
        if not self._current_reported:
            self._settle_silent_window()
        self._current_segment = segment
        self._current_reported = False

    def update(self, amount: object) -> None:
        if not self._total or self._finished:
            return
        try:
            delta = max(0, int(amount))
        except (TypeError, ValueError, OverflowError):
            delta = 0
        self._advanced = min(self._total, self._advanced + delta)
        self._current_reported = True
        self._emit()

    def finish(self, *, success: bool) -> None:
        if self._finished:
            return
        self._finished = True
        if not success or not self._total:
            return
        if self._current_segment is not None and not self._current_reported:
            self._settle_silent_window(force=True)
        # 本桥只服务 Lulu 的完整音频入口；原函数正常返回才补齐最后的 100%。
        self._advanced = self._total
        self._emit(force=True)

    def _settle_silent_window(self, *, force: bool = False) -> None:
        remaining = max(0, self._total - self._advanced)
        self._advanced += min(self._window_frames, remaining)
        self._current_reported = True
        self._emit(force=force)

    def _emit(self, *, force: bool = False) -> None:
        if not self._total:
            return
        fraction = max(0.0, min(1.0, self._advanced / self._total))
        percent = min(100, int(fraction * 100))
        if not force and percent <= self._last_percent:
            return
        if force and percent == self._last_percent:
            return
        self._last_percent = percent
        try:
            self._callback(fraction)
        except InterruptedError:
            raise
        except Exception:
            # 进度是旁路，界面回调异常不能中断模型推理。
            return


class _ProgressBar:
    def __init__(self, controller: _ProgressController, total: object) -> None:
        self._controller = controller
        self._closed = False
        self._controller.start(total)

    def __enter__(self) -> _ProgressBar:
        return self

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        self.close(success=exc_type is None)
        return False

    def update(self, amount: object = 1) -> None:
        self._controller.update(amount)

    def close(self, *, success: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self._controller.finish(success=success)


class _TqdmModuleProxy:
    def __init__(
        self,
        original: object,
        controller: _ProgressController,
        *,
        owner_thread: int,
    ) -> None:
        self._original = original
        self._controller = controller
        self._owner_thread = owner_thread

    def tqdm(self, *args, **kwargs):
        if threading.get_ident() != self._owner_thread:
            return self._original.tqdm(*args, **kwargs)
        total = kwargs.get("total")
        if total is None and len(args) > 1:
            total = args[1]
        return _ProgressBar(self._controller, total)

    def __getattr__(self, name: str):
        return getattr(self._original, name)


class _ModelProxy:
    def __init__(self, model: object, controller: _ProgressController | None, check_cancel) -> None:
        self._model = model
        self._controller = controller
        self._check_cancel = check_cancel

    def decode(self, segment, options):
        # Independent of progress throttling, silence, and temperature retries.
        self._check_cancel()
        if self._controller is not None:
            self._controller.before_decode(segment)
        result = self._model.decode(segment, options)
        self._check_cancel()
        return result

    def __getattr__(self, name: str):
        return getattr(self._model, name)


def _compatible(module: ModuleType) -> bool:
    holder = getattr(module, "ModelHolder", None)
    descriptor = getattr(holder, "__dict__", {}).get("get_model")
    tqdm_module = getattr(module, "tqdm", None)
    try:
        window_frames = int(getattr(module, "N_FRAMES", 0))
    except (TypeError, ValueError, OverflowError):
        window_frames = 0
    return (
        holder is not None
        and isinstance(descriptor, classmethod)
        and callable(getattr(tqdm_module, "tqdm", None))
        and window_frames > 0
    )


def transcribe_with_audio_progress(
    audio: Any,
    *,
    path_or_hf_repo: str,
    on_audio_position: AudioPositionCallback,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """执行原始 Whisper MLX 转录，并旁路报告已处理音频比例。

    进度不可用时仍保留解码取消边界；取消合同也不兼容时，在推理前明确拒绝。
    桥接建立以后出现的推理异常按原样抛出，不重跑昂贵模型任务。
    """
    import mlx_whisper

    module = importlib.import_module("mlx_whisper.transcribe")
    upstream = mlx_whisper.transcribe
    def check_cancel():
        if is_cancelled is not None and is_cancelled():
            raise InterruptedError("转录已取消")

    check_cancel()
    with _BRIDGE_LOCK:
        check_cancel()
        holder = getattr(module, "ModelHolder", None)
        descriptor = getattr(holder, "__dict__", {}).get("get_model")
        if not isinstance(descriptor, classmethod):
            if is_cancelled is not None:
                raise RuntimeError("当前 Whisper 运行工具不支持安全取消，请修复运行环境后重试。")
            return upstream(audio, path_or_hf_repo=path_or_hf_repo)
        progress_available = _compatible(module)
        original_descriptor = holder.__dict__["get_model"]
        original_get_model = holder.get_model
        original_tqdm = getattr(module, "tqdm", None)
        owner_thread = threading.get_ident()
        controller = _ProgressController(
            on_audio_position,
            window_frames=module.N_FRAMES,
        ) if progress_available else None

        def get_model(cls, model_path: str, dtype):
            if threading.get_ident() == owner_thread:
                check_cancel()
            model = original_get_model(model_path, dtype)
            if threading.get_ident() != owner_thread:
                return model
            check_cancel()
            return _ModelProxy(model, controller, check_cancel)

        patched_holder = False
        patched_tqdm = False
        try:
            holder.get_model = classmethod(get_model)
            patched_holder = True
            if controller is not None:
                module.tqdm = _TqdmModuleProxy(
                    original_tqdm,
                    controller,
                    owner_thread=owner_thread,
                )
                patched_tqdm = True
        except Exception:
            if patched_tqdm:
                module.tqdm = original_tqdm
            if patched_holder:
                holder.get_model = original_descriptor
            if is_cancelled is not None:
                raise RuntimeError("Whisper 取消保护未能准备，转录未开始；请修复运行环境。")
            return upstream(audio, path_or_hf_repo=path_or_hf_repo)

        try:
            check_cancel()
            result = upstream(audio, path_or_hf_repo=path_or_hf_repo)
            check_cancel()
            return result
        finally:
            if patched_tqdm:
                module.tqdm = original_tqdm
            holder.get_model = original_descriptor
