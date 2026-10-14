"""Phase 1A 可由正式 GUI 调用的无 Qt 本地转录 Application Service。"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat
from typing import Protocol

from .auth_contracts import AuthorizationError
from .auth_guard import OpenProductAccessGuard, UnifiedLicenseAuthGuard
from .collect.preferences import CollectCoverPreferenceSnapshot
from .models import Transcript
from .pipeline import transcribe_source, was_cancelled
from .transcription_contracts import (
    LocalFileTranscriptionFailureV1,
    LocalFileTranscriptionRequestV1,
    LocalFileTranscriptionResultV1,
    LocalTranscriptionSegmentV1,
)


from .local_transcription_models import (
    DEFAULT_ENGINE, LocalModelUnavailable, normalize_engine, probe_local_model_readiness,
)

ProgressSink = Callable[[str, float], None]
CancellationProbe = Callable[[], bool]


class LocalFileTranscriptionError(Exception):
    """只携带一个可向用户呈现的版本化失败 DTO。"""

    def __init__(self, failure: LocalFileTranscriptionFailureV1):
        if type(failure) is not LocalFileTranscriptionFailureV1:
            raise TypeError("failure 必须是 LocalFileTranscriptionFailureV1")
        self.failure = failure
        super().__init__(failure.user_message)


class LocalTranscriptionPipelineRunner(Protocol):
    def __call__(
        self,
        source_path: str,
        *,
        engine_name: str,
        progress: ProgressSink | None,
        is_cancelled: CancellationProbe,
    ) -> Transcript: ...


class LocalModelReadinessProbe(Protocol):
    def __call__(
        self,
        engine_name: str,
    ) -> bool: ...


def run_local_transcription_pipeline_adapter(
    source_path: str,
    *,
    engine_name: str,
    progress: ProgressSink | None,
    is_cancelled: CancellationProbe,
) -> Transcript:
    engine_name = normalize_engine(engine_name)
    return transcribe_source(
        source_path,
        engine_name=engine_name,
        enable_link_download=False,
        keep_media=False,
        media_dir=None,
        link_media_mode="audio",
        prefer_subtitles=False,
        progress=progress,
        is_cancelled=is_cancelled,
        cover_snapshot=CollectCoverPreferenceSnapshot(False),
        cover_workflow=None,
        execution_policy=None,
    )


# Compatibility import for existing callers; all registered local engines are supported.
probe_local_sensevoice_readiness_adapter = probe_local_model_readiness


class LocalFileTranscriptionApplicationService:
    def __init__(
        self,
        authorization_guard: OpenProductAccessGuard | None,
        *,
        pipeline_runner: LocalTranscriptionPipelineRunner = run_local_transcription_pipeline_adapter,
        model_readiness_probe: LocalModelReadinessProbe = probe_local_model_readiness,
    ) -> None:
        self._authorization_guard = authorization_guard
        self._pipeline_runner = pipeline_runner
        self._model_readiness_probe = model_readiness_probe

    def transcribe(
        self,
        request: LocalFileTranscriptionRequestV1,
        *,
        progress: ProgressSink | None = None,
        is_cancelled: CancellationProbe | None = None,
    ) -> LocalFileTranscriptionResultV1:
        self._assert_authorized(request)
        source_path = self._validate_request_and_source(request)
        cancellation_probe = is_cancelled or _never_cancelled
        if not callable(cancellation_probe):
            raise _failure("INVALID_ARGUMENT")
        if _read_cancellation(cancellation_probe):
            raise _failure("CANCELLED")

        engine_name = normalize_engine(request.engine_name)
        try:
            ready = bool(self._model_readiness_probe(engine_name))
        except LocalFileTranscriptionError:
            raise
        except LocalModelUnavailable as exc:
            failure = LocalFileTranscriptionError(LocalFileTranscriptionFailureV1(exc.code, str(exc)))
            failure.reason_code = exc.reason_code
            raise failure from exc
        except Exception as exc:  # noqa: BLE001
            raise _failure("INTERNAL_ERROR") from exc
        if not ready:
            raise _failure("MODEL_NOT_READY")

        try:
            transcript = self._pipeline_runner(
                source_path,
                engine_name=engine_name,
                progress=progress,
                is_cancelled=cancellation_probe,
            )
        except LocalFileTranscriptionError:
            raise
        except Exception as exc:  # noqa: BLE001
            if was_cancelled(exc):
                raise _failure("CANCELLED") from exc
            raise _failure("INTERNAL_ERROR") from exc

        if _read_cancellation(cancellation_probe):
            raise _failure("CANCELLED")
        try:
            return _result_from_transcript(
                transcript,
                source_path=request.source_path,
                engine_name=engine_name,
            )
        except LocalFileTranscriptionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _failure("INTERNAL_ERROR") from exc

    def _assert_authorized(self, request: object) -> None:
        guard = self._authorization_guard
        # The legacy guard remains for isolated historical/safe-E2E callers.
        # Official GUI/Core factories create only OpenProductAccessGuard.
        if type(guard) not in (OpenProductAccessGuard, UnifiedLicenseAuthGuard):
            raise _failure("AUTH_REQUIRED")
        try:
            guard.assert_local_file_transcription_allowed(request)
        except AuthorizationError as exc:
            raise _failure(exc.failure.code) from exc

    @staticmethod
    def _validate_request_and_source(
        request: object,
    ) -> str:
        if type(request) is not LocalFileTranscriptionRequestV1:
            raise _failure("INVALID_ARGUMENT")
        if (
            not isinstance(request.source_path, str)
            or not request.source_path
            or request.source_path.lower().startswith(("http://", "https://"))
        ):
            raise _failure("INVALID_ARGUMENT")
        try:
            normalize_engine(request.engine_name)
        except ValueError as exc:
            raise _failure("INVALID_ARGUMENT") from exc
        source_path = request.source_path
        try:
            source_stat = os.lstat(source_path)
        except FileNotFoundError as exc:
            raise _failure("NOT_FOUND") from exc
        except OSError as exc:
            raise _failure("UNSAFE_PATH") from exc
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise _failure("UNSAFE_PATH")
        if not os.access(source_path, os.R_OK):
            raise _failure("UNSAFE_PATH")
        return str(Path(source_path))


def _read_cancellation(probe: CancellationProbe) -> bool:
    try:
        return bool(probe())
    except LocalFileTranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _failure("INTERNAL_ERROR") from exc


def _never_cancelled() -> bool:
    return False


def _result_from_transcript(
    transcript: Transcript,
    *,
    source_path: str,
    engine_name: str = DEFAULT_ENGINE,
) -> LocalFileTranscriptionResultV1:
    if not isinstance(transcript, Transcript) or not isinstance(transcript.text, str):
        raise _failure("INTERNAL_ERROR")
    if transcript.engine != engine_name:
        raise _failure("INTERNAL_ERROR")
    segments = tuple(
        LocalTranscriptionSegmentV1(
            text=segment.text,
            start=segment.start,
            end=segment.end,
        )
        for segment in transcript.segments
    )
    return LocalFileTranscriptionResultV1(
        text=transcript.text,
        segments=segments,
        language=transcript.language,
        engine=engine_name,
        source_path=source_path,
    )


def _failure(code: str) -> LocalFileTranscriptionError:
    messages = {
        "AUTH_REQUIRED": "当前工作台不可用，请重新打开 Lulu 后重试。",
        "SECURE_STORE_UNAVAILABLE": "系统安全存储不可用，请在可信 GUI 中检查授权状态后重试。",
        "INVALID_ARGUMENT": "转录请求无效，请选择本地音频或视频文件后重试。",
        "NOT_FOUND": "找不到要转录的本地文件，请重新选择。",
        "UNSAFE_PATH": "该文件路径不安全或不可读，请选择普通本地文件。",
        "MODEL_NOT_READY": "所选本地转录模型尚未就绪，请先在 Lulu 中完成模型准备。",
        "RUNTIME_NOT_READY": "所选本地转录模型的运行环境不可用，请在 Lulu 中检查。",
        "CANCELLED": "转录已取消。",
        "INTERNAL_ERROR": "转录失败，请稍后重试；若问题持续，请重新选择文件。",
    }
    if code not in messages:
        code = "INTERNAL_ERROR"
    return LocalFileTranscriptionError(
        LocalFileTranscriptionFailureV1(
            code=code,  # type: ignore[arg-type]
            user_message=messages[code],
        )
    )
