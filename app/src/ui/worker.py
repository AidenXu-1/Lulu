"""后台转录线程：把耗时的 pipeline 放到 QThread，避免卡 UI。

通过信号把进度 / 成功 / 失败回传到主线程。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.collect.preferences import CollectCoverPreferenceSnapshot
from ..core.models import Transcript
from ..core.pipeline import SingleLinkCoverWorkflow, transcribe_source, was_cancelled
from ..core.synthesis.base import SynthesisRequest
from ..core.synthesis.indextts2_subprocess import (
    IndexTTS2SubprocessEngine,
    cancel_persistent_worker,
    check_runtime,
    persistent_worker_status,
    shutdown_persistent_worker,
    was_cancelled as indextts2_was_cancelled,
)
from ..core.synthesis.indextts2_presets import emotion_weight_for, normalize_emotion_intensity
from ..core.synthesis.indextts2_readiness import sync_official_config
from ..core.synthesis.pipeline import synthesize_text
from ..core.synthesis.errors import user_error_message
from ..core.synthesis import minimax_tts
from ..core.synthesis.voices import get_voice
from ..core.synthesis.voxcpm2 import was_cancelled as synthesis_was_cancelled


class TranscribeWorker(QThread):
    # （人类可读说明， 进度 0~1;<0 表示不确定）
    progress = Signal(str, float)
    finished_ok = Signal(object)   # Transcript
    failed = Signal(str)
    cancelled = Signal()
    stage_changed = Signal(str)

    def __init__(
        self,
        source: str,
        engine_name: str,
        enable_link_download: bool,
        keep_media: bool = False,
        media_dir: str | None = None,
        link_media_mode: str = "audio",
        prefer_subtitles: bool = False,
        cover_preference_service=None,
        cover_workflow: SingleLinkCoverWorkflow | None = None,
        execution_policy=None,
        local_file_transcription_authorization_guard=None,
        local_file_transcription_service=None,
    ):
        super().__init__()
        self._source = source
        self._engine_name = engine_name
        self._enable_link = enable_link_download
        self._keep_media = keep_media
        self._media_dir = media_dir
        self._link_media_mode = link_media_mode
        self._prefer_subtitles = prefer_subtitles
        self._execution_policy = execution_policy
        self._local_file_transcription_authorization_guard = (
            local_file_transcription_authorization_guard
        )
        self._local_file_transcription_service = local_file_transcription_service
        try:
            snapshot = (
                cover_preference_service.snapshot()
                if cover_preference_service is not None
                else CollectCoverPreferenceSnapshot(False)
            )
        except Exception:
            snapshot = CollectCoverPreferenceSnapshot(False)
        self._cover_snapshot = (
            snapshot
            if isinstance(snapshot, CollectCoverPreferenceSnapshot)
            else CollectCoverPreferenceSnapshot(False)
        )
        self._cover_workflow = cover_workflow
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        return self._cancel or self.isInterruptionRequested()

    def _uses_local_file_transcription_service(self) -> bool:
        from ..core.engines.local_models import LOCAL_MODELS
        if self._execution_policy is not None and self._execution_policy.requested_outputs is not None:
            return False
        if (
            self._local_file_transcription_authorization_guard is None
            or self._engine_name not in LOCAL_MODELS
        ):
            return False
        from ..core.audio.extractor import extract_url

        return extract_url(self._source) is None

    def _run_local_file_transcription_service(self) -> None:
        from ..core.transcription_application import (
            LocalFileTranscriptionApplicationService,
            LocalFileTranscriptionError,
        )
        from ..core.transcription_contracts import LocalFileTranscriptionRequestV1

        service = self._local_file_transcription_service
        if service is None:
            service = LocalFileTranscriptionApplicationService(
                self._local_file_transcription_authorization_guard
            )
        try:
            result = service.transcribe(
                LocalFileTranscriptionRequestV1(
                    source_path=self._source,
                    engine_name=self._engine_name,
                ),
                progress=lambda msg, frac: self.progress.emit(msg, frac),
                is_cancelled=self._is_cancelled,
            )
        except LocalFileTranscriptionError as exc:
            trace = getattr(self, "_attempt_trace", None)
            if trace:
                trace.returned("cancelled" if exc.failure.code == "CANCELLED" else "failed", exc)
            if exc.failure.code == "CANCELLED":
                self.cancelled.emit()
            else:
                self.failed.emit(exc.failure.user_message)
            return
        trace = getattr(self, "_attempt_trace", None)
        if trace:
            trace.returned("succeeded")
        self.finished_ok.emit(result.to_transcript())

    def run(self) -> None:  # 在子线程里执行
        from ..core.engines.asr_resource import batch_context
        with batch_context(getattr(self, '_asr_batch_owner', None)):
            self._run_transcription()

    def _run_transcription(self) -> None:
        from ..core.diagnostics import TranscriptionAttemptTrace
        trace = TranscriptionAttemptTrace(self.audit_job_id) if hasattr(self, "audit_job_id") else None
        self._attempt_trace = trace
        def stage_changed(stage):
            if stage in TranscriptionAttemptTrace.STAGES:
                self.stage_changed.emit(stage)
                if trace:
                    trace.enter(stage)
        try:
            if self._uses_local_file_transcription_service():
                stage_changed("local_service")
                self._run_local_file_transcription_service()
                return
            acquire = transcribe_source
            if self._execution_policy is not None and self._execution_policy.requested_outputs is not None:
                from ..core.extraction import acquire_source
                acquire = acquire_source
            transcript: Transcript = acquire(
                self._source,
                engine_name=self._engine_name,
                enable_link_download=self._enable_link,
                keep_media=self._keep_media,
                media_dir=self._media_dir,
                link_media_mode=self._link_media_mode,
                prefer_subtitles=self._prefer_subtitles,
                progress=lambda msg, frac: self.progress.emit(msg, frac),
                is_cancelled=self._is_cancelled,
                cover_snapshot=self._cover_snapshot,
                cover_workflow=self._cover_workflow,
                execution_policy=self._execution_policy,
                stage_changed=stage_changed,
            )
            if self._is_cancelled():
                if trace:
                    trace.returned("cancelled")
                self.cancelled.emit()
                return
            if trace:
                trace.returned("succeeded")
            self.finished_ok.emit(transcript)
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.returned("cancelled" if was_cancelled(exc) or self._is_cancelled() else "failed", exc)
            if was_cancelled(exc) or self._is_cancelled():
                self.cancelled.emit()
            else:
                from ..core.diagnostics import record_failure
                record_failure("transcription", exc)
                self.failed.emit(str(exc))


class SynthesisWorker(QThread):
    progress = Signal(str, float)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, text: str, voice_id: str, output_path: str, output_format: str,
                 engine: str | None = None, engine_options: dict | None = None, voice_profile=None):
        super().__init__()
        self._text = text
        self._voice_id = voice_id
        self._output_path = output_path
        self._output_format = output_format
        self._engine = engine
        self._engine_options = engine_options or {}
        self._voice_profile = voice_profile
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            result = synthesize_text(
                self._text,
                self._voice_id,
                self._output_path,
                self._output_format,
                progress=lambda msg, frac: self.progress.emit(msg, frac),
                is_cancelled=lambda: self._cancel,
                engine=self._engine,
                engine_options=self._engine_options,
                voice_profile=self._voice_profile,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            if synthesis_was_cancelled(exc):
                self.cancelled.emit()
            else:
                self.failed.emit(user_error_message(exc))


class MiniMaxTTSWorker(QThread):
    progress = Signal(str, float)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: minimax_tts.MiniMaxTtsRequest):
        super().__init__()
        self._request = request
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            result = minimax_tts.synthesize(
                self._request,
                progress=lambda msg, frac: self.progress.emit(msg, frac),
                is_cancelled=lambda: self._cancel,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, minimax_tts.MiniMaxTtsError) and exc.code == "CANCELLED":
                self.cancelled.emit()
            else:
                self.failed.emit(minimax_tts.safe_error_message(exc))


class MiniMaxConfigStatusWorker(QThread):
    """Read only the boolean MiniMax configured state away from the Qt UI thread."""

    def __init__(self, generation: int):
        super().__init__()
        self.generation = int(generation)
        self.configured: bool | None = None
        self.failed = False

    def run(self) -> None:
        try:
            self.configured = bool(minimax_tts.is_configured())
        except Exception:  # noqa: BLE001 - UI receives no backend or secret details
            self.configured = None
            self.failed = True


class MiniMaxVoiceRefreshWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.finished_ok.emit(minimax_tts.refresh_voice_catalog())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(minimax_tts.safe_error_message(exc))


class MiniMaxCloneVoiceWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, request: minimax_tts.MiniMaxCloneRequest):
        super().__init__()
        self._request = request
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        self._request.is_cancelled = lambda: self._cancel
        try:
            self.finished_ok.emit(minimax_tts.clone_voice(self._request))
        except Exception as exc:  # noqa: BLE001
            msg = minimax_tts.safe_error_message(exc)
            if isinstance(exc, minimax_tts.MiniMaxTtsError) and exc.code == "CANCELLED":
                self.cancelled.emit(msg)
            else:
                self.failed.emit(msg)


class MiniMaxDeleteVoiceWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, voice_id: str, *, delete_remote: bool):
        super().__init__()
        self._voice_id = voice_id
        self._delete_remote = delete_remote

    def run(self) -> None:
        try:
            result = minimax_tts.delete_cloned_voice(self._voice_id, delete_remote=self._delete_remote)
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(minimax_tts.safe_error_message(exc))


class IndexTTS2SynthesisWorker(QThread):
    progress = Signal(str, float)
    lifecycle = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        emotion_preset: str,
        style_preset: dict | None = None,
        emotion_intensity: str = "standard",
        emotion_vector: list[float] | tuple[float, ...] | None = None,
        emotion_weight: float | None = None,
        emotion_summary: str = "",
        emotion_source_preset: str = "",
        emotion_source_intensity: str = "",
        text_length: dict | None = None,
        text_segments: list[str] | tuple[str, ...] | None = None,
        emotion_mode: str = "",
        emotion_audio_path: str = "",
        emotion_text: str = "",
        language: str = "ZH",
        speed: float = 1.0,
        preview: bool = False,
        random_emotion: bool = False,
        model_version: str = "2.5",
        generation_options: dict | None = None,
    ):
        super().__init__()
        self._text = text
        self._voice_id = voice_id
        self._output_path = output_path
        self._emotion_preset = emotion_preset
        self._style_preset = dict(style_preset or {})
        self._emotion_intensity = normalize_emotion_intensity(emotion_intensity, emotion_preset)
        self._emotion_vector = list(emotion_vector) if emotion_vector is not None else None
        self._emotion_weight = emotion_weight
        self._emotion_summary = emotion_summary
        self._emotion_source_preset = emotion_source_preset
        self._emotion_source_intensity = emotion_source_intensity
        self._text_length = dict(text_length or {})
        self._text_segments = [str(seg).strip() for seg in text_segments or [] if str(seg).strip()]
        # 情感控制方式（五选一）:follow/emoaudio/template/vector/emotext。空字符串时由
        # adapter 按是否有自定义向量回退，保持旧调用向后兼容。
        self._emotion_mode = str(emotion_mode or "").strip().lower()
        self._emotion_audio_path = str(emotion_audio_path or "")
        self._emotion_text = str(emotion_text or "")
        self._language = language
        self._speed = speed
        self._preview = preview
        self._random_emotion = random_emotion
        if model_version not in ("2.0", "2.5"):
            raise ValueError("不支持的 IndexTTS 版本")
        self._model_version = model_version
        self._generation_options = dict(generation_options or {})
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True
        cancel_persistent_worker()

    def run(self) -> None:
        try:
            voice = get_voice(self._voice_id)
            if voice is None:
                raise RuntimeError("请选择一个已保存的音色。")
            emotion_weight = (
                float(self._emotion_weight)
                if self._emotion_weight is not None
                else emotion_weight_for(self._emotion_intensity, self._emotion_preset)
            )
            engine_options = {
                "emotion_preset": self._emotion_preset,
                "emotion_intensity": self._emotion_intensity,
                "emotion_weight": emotion_weight,
                "style_preset": self._style_preset,
                "text_length": self._text_length,
                "text_segments": list(self._text_segments),
                "timeout_seconds": 1800,
                "use_persistent_worker": True,
                "language": self._language,
                "speed": self._speed,
                "preview": self._preview,
                "random_emotion": self._random_emotion,
                "model_version": self._model_version,
                "device": "cpu",
            }
            if self._model_version in ('2.0', '2.5') and self._generation_options:
                engine_options['generation_options'] = dict(self._generation_options)
            if self._emotion_mode:
                engine_options["emotion_mode"] = self._emotion_mode
            if self._emotion_audio_path:
                engine_options["emotion_audio_path"] = self._emotion_audio_path
            if self._emotion_text:
                engine_options["emotion_text"] = self._emotion_text
            if self._emotion_vector is not None:
                engine_options.update(
                    {
                        "emotion_vector": list(self._emotion_vector),
                        "emotion_summary": self._emotion_summary,
                        "emotion_source_preset": self._emotion_source_preset or self._emotion_preset,
                        "emotion_source_intensity": self._emotion_source_intensity or self._emotion_intensity,
                    }
                )
            request = SynthesisRequest(
                text=self._text,
                voice=voice,
                output_path=Path(self._output_path),
                output_format="wav",
                engine_options=engine_options,
            )
            result = IndexTTS2SubprocessEngine().synthesize(
                request,
                progress=lambda msg, frac: self.progress.emit(msg, frac),
                is_cancelled=lambda: self._cancel,
            )
            self.lifecycle.emit(persistent_worker_status())
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            if self._cancel or indextts2_was_cancelled(exc):
                self.lifecycle.emit(persistent_worker_status())
                self.cancelled.emit()
            else:
                self.lifecycle.emit(persistent_worker_status())
                self.failed.emit(user_error_message(exc, "IndexTTS2 生成失败，请检查本地环境和音色素材。"))


class IndexTTS2BackgroundReleaseWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            shutdown_persistent_worker()
            self.finished_ok.emit(persistent_worker_status())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_error_message(exc, "IndexTTS2 后台关闭失败，请稍后重试。"))


class IndexTTS2StatusWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, model_version: str = "2.5"):
        super().__init__()
        self.model_version = model_version

    def run(self) -> None:
        try:
            self.finished_ok.emit(check_runtime(expected_version=self.model_version))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_error_message(exc, "状态检查失败，请重试。"))


class IndexTTS2ConfigSyncWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.finished_ok.emit(sync_official_config())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(user_error_message(exc, "配置同步失败，请检查本机文件权限后重试。"))


class ReferenceTranscribeWorker(QThread):
    progress = Signal(str, float)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, audio_path: str):
        super().__init__()
        self._audio_path = audio_path

    def run(self) -> None:
        try:
            transcript = transcribe_source(
                self._audio_path,
                engine_name="local_sensevoice_q8",
                enable_link_download=False,
                is_cancelled=self.isInterruptionRequested,
                progress=lambda msg, frac: self.progress.emit(msg, frac),
            )
            self.finished_ok.emit(transcript.text.strip())
        except Exception:  # noqa: BLE001
            self.failed.emit("参考音频转写失败，请手动填写参考文本后再保存。")


class TTSRuntimeInstallWorker(QThread):
    """后台安装本地配音模型的独立运行时包（下环境包 + 权重），可取消。"""

    progress = Signal(str, float)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, model_id: str, *, local_archive: Path | None = None):
        super().__init__()
        self.model_id = model_id
        self.local_archive = local_archive
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from ..core.synthesis import runtime_packages as rp
        from ..core import model_transfer as transfer
        def check():
            if self._cancel:
                raise rp.CancelledError()
        try:
            with transfer.Session(self.progress.emit, check) as session:
                transfer.prepare_tts(session, self.model_id, self.local_archive)
                rp.install(
                    self.model_id,
                    progress=lambda m, _f: self.progress.emit(m, -1),
                    is_cancelled=lambda: self._cancel,
                    **({"local_archive": self.local_archive} if self.local_archive else {}),
                )
        except rp.CancelledError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        else:
            self.finished_ok.emit()


class QwenFeatureWorker(QThread):
    progress = Signal(str, float)
    failed = Signal(str)

    def __init__(self, name=None, *, operation="install", local_archive=None, retain_common=False):
        super().__init__()
        self.name=name; self.operation=operation; self.local_archive=local_archive
        self.retain_common=retain_common; self._cancel=False; self.error=None; self.was_cancelled=False; self.result=None

    def request_cancel(self): self._cancel=True

    def run(self):
        from ..core.synthesis import qwen3_deployment as deployment, runtime_packages as rp
        try:
            if self.operation == "plan": self.result=deployment.plan(self.name, cancel=lambda:self._cancel)
            elif self.operation in {"removal_plan", "cancel_plan"}: self.result=deployment.removal_plan(self.name)
            elif self.operation in {"uninstall", "cleanup"}:
                deployment.remove(self.name, retain_common=self.retain_common)
            else:
                from ..core import model_transfer as transfer
                def check():
                    if self._cancel: raise rp.CancelledError()
                with transfer.Session(self.progress.emit, check) as session:
                    names = [n for n,v in deployment.read_state()['features'].items() if v['state'] in {'queued','installing'}]
                    transfer.prepare_tts(session, 'local_qwen3_tts', self.local_archive, names)
                    deployment.run(lambda m,f:self.progress.emit(m,-1), lambda:self._cancel, local_archive=self.local_archive)
                self.was_cancelled=self._cancel
        except rp.CancelledError: self.was_cancelled=True
        except Exception as exc: self.error=str(exc)
        finally:
            if self.operation == "install" and (self.error or self.was_cancelled):
                try:
                    with deployment.edit_state(deployment.root()) as state:
                        for value in state["features"].values():
                            if value.get("session") == deployment.SESSION and value.get("state") in {"queued", "installing"}:
                                value["state"] = "failed" if self.error else "paused"
                                if self.error:
                                    value["error"] = self.error
                except Exception:
                    self.error = (self.error or "安装已暂停。") + " 安装状态未能保存，请检查磁盘空间和权限后重试。"


class ModelRemoveWorker(QThread):
    settled = Signal(str)

    def __init__(self, model_id: str, *, tts: bool = False):
        super().__init__()
        self.model_id = model_id
        self.tts = tts

    def run(self):
        try:
            if self.tts:
                from ..core.synthesis import runtime_packages
                runtime_packages.uninstall(self.model_id, remove_weights=True)
            else:
                from ..core import models_manager
                models_manager.delete(self.model_id)
        except Exception as exc:
            self.settled.emit(str(exc))
        else:
            self.settled.emit("")


class ModelsRelocateWorker(QThread):
    """后台把本地模型（models/ + tts/）移动到新位置，移完保存配置。"""

    progress = Signal(str, float)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, new_root, cfg):
        super().__init__()
        self.new_root = new_root
        self.cfg = cfg

    def run(self) -> None:
        from ..core import models_manager as mm
        try:
            mm.relocate_and_save(self.new_root, self.cfg, progress=lambda m, f: self.progress.emit(m, f))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit()


class CoreTaskSaveWorker(QThread):
    """Only immutable task snapshots cross into the serial Core writer."""
    settled = Signal(object, bool)

    def __init__(self, store, tasks, groups, parent=None):
        super().__init__(parent)
        from copy import copy, deepcopy
        self.store = store
        self.tasks = [copy(task) for task in tasks]
        self.groups = deepcopy(groups)
        self.ok = False
        self.error_code = "CORE_OPERATION_FAILED"
        self.failed_task_id = None

    def run(self):
        ok = False
        try:
            ok = bool(self.store.save(self.tasks, self.groups))
        except Exception as exc:
            from ..core.diagnostics import failure_code, record_failure
            record_failure("gui_task_save", exc)
            self.failed_task_id = getattr(exc, "task_id", None)
            # Closed codes only; neither Core reply text nor input is shown.
            for _ in range(6):
                if exc is None:
                    break
                self.error_code = failure_code(exc)
                exc = exc.__cause__
        self.ok = ok
        self.settled.emit(self.tasks, ok)


class CoreTaskStartupWorker(QThread):
    """Prepare the default measured task connection without blocking painting."""
    ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, parent=None, *, store=None):
        super().__init__(parent)
        self._store = store

    def run(self) -> None:
        from ..core.gui_task_ledger import CoreTaskQueueStore
        # Only this state read can reconnect. Creation/start/save requests are
        # never replayed here, including when their reply was lost.
        for attempt in range(3):
            if self.isInterruptionRequested():
                return
            stage = "connect"
            try:
                store = self._store if self._store is not None else CoreTaskQueueStore.production()
                if self.isInterruptionRequested():
                    return
                stage = "load"
                snapshot = store.load()
                if not self.isInterruptionRequested():
                    self.ready.emit(store, snapshot)
                return
            except Exception as exc:
                if self.isInterruptionRequested():
                    return
                from ..core.diagnostics import record_failure
                record_failure("gui_task_connect", exc)
                code = getattr(exc, "native_code", None)
                if type(code) is int and code in {20, 21} and attempt < 2:
                    self.msleep(250 * (attempt + 1))
                    continue
                # Closed diagnostics only, without request/credential contents.
                native = str(code) if type(code) is int and code in {2, 10, 11, 12, 20, 21, 22, 30} else "unknown"
                self.failed.emit(f"CORE_TASK_SERVICE_UNAVAILABLE:{stage}:{native}")
                return


class CoreTaskResultWorker(QThread):
    ready = Signal(object)
    failed = Signal()

    def __init__(self, store, job_id, result_ref, parent=None):
        super().__init__(parent)
        self._store, self._job_id, self._result_ref = store, job_id, result_ref

    def run(self) -> None:
        try:
            transcript = self._store.read_transcript(self._job_id, self._result_ref, is_cancelled=self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.ready.emit(transcript)
        except Exception:
            if not self.isInterruptionRequested():
                self.failed.emit()
