"""主链路编排:输入(文件/链接) → 取音频 → 转码 → 引擎转录 → Transcript。

这一层不依赖 UI,也可被脚本直接调用(方便测试与无界面跑批)。
写出文件由调用方用 output.writer 完成(UI 里导出时机由用户决定)。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
import inspect
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Protocol

from .audio.converter import to_wav_16k_mono
from .audio.extractor import extract_url, resolve_source
from .collect.cover import COVER_FAILURE_CODES
from .collect.media import (
    ManagedMediaValidation,
    resolve_managed_transcription_media,
)
from .collect.preferences import CollectCoverPreferenceSnapshot
from .engines.base import ProgressCallback, _noop
from .engines.registry import get_engine
from .models import Transcript, TranscriptCover
from .progress import stage_callback
from .task import TaskExecutionPolicy

# 转录临时目录的前缀(下载/转码都放这种目录,转完即焚)
_TMP_PREFIX = "bzl_"
_COVER_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SingleLinkCoverWorkflow(Protocol):
    """C2 注入边界；生产入口在 C3 前不提供实现。"""

    def localize(
        self,
        *,
        platform: str,
        work_id: str,
        url: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> object: ...


def cleanup_stale_temp(older_than_seconds: int = 3600) -> int:
    """只清理所有者已退出的旧目录；未知归属的历史目录保留。"""
    base = Path(tempfile.gettempdir())
    now = time.time()
    removed = 0
    try:
        for d in base.glob(f"{_TMP_PREFIX}*"):
            try:
                if d.is_symlink() or not d.is_dir() or now - d.stat().st_mtime <= older_than_seconds:
                    continue
                owner = d / ".owner-pid"
                if owner.is_symlink() or not owner.is_file() or owner.stat().st_size > 32:
                    continue
                pid = int(owner.read_text(encoding="ascii"))
                if pid <= 0:
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    shutil.rmtree(d)
                    removed += 1
                # ponytail: PID 被复用时宁可保留；误保留不会破坏正在工作的任务。
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return removed


@contextmanager
def _transcription_tempdir():
    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        path = Path(tmp)
        (path / ".owner-pid").write_text(str(os.getpid()), encoding="ascii")
        yield path


def transcribe_source(
    source: str,
    engine_name: str | None = None,
    enable_link_download: bool = True,
    keep_media: bool = False,
    media_dir: str | None = None,
    link_media_mode: str = "audio",
    prefer_subtitles: bool = False,
    subtitle_fallback: str = "local_transcription",
    progress: ProgressCallback | None = None,
    is_cancelled=None,
    cover_snapshot: CollectCoverPreferenceSnapshot | None = None,
    cover_workflow: SingleLinkCoverWorkflow | None = None,
    execution_policy: TaskExecutionPolicy | None = None,
    stage_changed: Callable[[str], None] | None = None,
    required_format: str | None = None,
) -> Transcript:
    """端到端把一个输入转成 Transcript。

    参数:
        source: 本地文件路径,或作品链接。
        engine_name: 引擎标识(默认本地 SenseVoice)。
        enable_link_download: 是否允许链接下载(关掉则只接受本地文件)。
        keep_media: 从链接下载时,是否把原始音视频另存到 media_dir(默认转完即焚)。
        media_dir: keep_media 时的保存目录。
        link_media_mode: 链接下载模式。audio=仅音频(更快),video=音视频(保留原视频)。
        progress: 进度回调 (msg, frac)。
        is_cancelled: 可选的取消查询函数,返回 True 时在阶段边界中止。
        cover_snapshot: worker 在任务启动时冻结的封面偏好。
        cover_workflow: C3 前仅测试注入；默认 None，不会发起真实封面请求。
    """
    stage_changed = stage_changed or (lambda _stage: None)
    report_progress = progress or _noop
    is_cancelled = is_cancelled or (lambda: False)
    def progress(message, fraction):
        if is_cancelled():
            raise _Cancelled()
        report_progress(message, fraction)
    if subtitle_fallback not in {"local_transcription", "fail"}:
        raise ValueError("subtitle_fallback 必须是 local_transcription 或 fail")
    if execution_policy is not None:
        if not isinstance(execution_policy, TaskExecutionPolicy):
            raise TypeError("execution_policy 必须是 TaskExecutionPolicy")
        engine_name = execution_policy.engine_name or engine_name
        material = execution_policy.material_snapshot
        link_media_mode = material.download_mode
        keep_media = bool(
            execution_policy.mode == "materialize"
            and execution_policy.media_destination == "user_directory"
            and material.save_media
        )
        cover_snapshot = CollectCoverPreferenceSnapshot(
            execution_policy.mode == "materialize" and material.save_cover
        )
        if execution_policy.media_directory is not None:
            media_dir = execution_policy.media_directory
        if execution_policy.prefer_subtitles is not None:
            prefer_subtitles = execution_policy.prefer_subtitles
        if execution_policy.local_only:
            from .engines.local_models import LOCAL_MODELS
            from .engines.providers import DEFAULT_ENGINE
            if (engine_name or DEFAULT_ENGINE) not in LOCAL_MODELS:
                raise ValueError('此任务仅支持本地转录，请选择 Q8、Whisper 或 MOSS 后重试；原录像保留。')
    if cover_snapshot is None:
        cover_snapshot = CollectCoverPreferenceSnapshot(False)
    if not isinstance(cover_snapshot, CollectCoverPreferenceSnapshot):
        raise TypeError("cover_snapshot 必须是 CollectCoverPreferenceSnapshot")
    is_link = extract_url(source) is not None
    input_progress = stage_callback(progress, 0.02, 0.20)
    cover_progress = stage_callback(progress, 0.12, 0.20)
    engine_progress = stage_callback(progress, 0.32, 0.95)

    def _check():
        if is_cancelled():
            raise _Cancelled()

    with ExitStack() as input_lease:
        if execution_policy is not None and execution_policy.origin == "homepage_batch_transcribe":
            from .collect.media_coordination import prepare_media
            stage_changed("waiting_media")
            input_lease.enter_context(prepare_media(execution_policy.stable_key, lambda: (_check() or False)))
        stage_changed("reuse_check")
        managed_input = ManagedMediaValidation(False)
        if (
            execution_policy is not None
            and execution_policy.origin == "homepage_batch_transcribe"
        ):
            _check()
            progress("检查已下载音视频…", 0.02)
            managed_input = resolve_managed_transcription_media(
                execution_policy.stable_key
            )
            _check()

        # 0) 链接 + 开了"优先用平台字幕":有字幕就直接用(跳过下载/转码/模型)
        if (
            not managed_input.valid
            and is_link
            and prefer_subtitles
            and enable_link_download
        ):
            from .subtitles import fetch_from_link
            stage_changed("subtitles")
            sub = fetch_from_link(source, stage_callback(progress, 0.02, 0.88))
            _check()
            if sub is not None:
                sub.source = source
                sub.meta = _settle_single_link_cover(
                    sub.meta,
                    snapshot=cover_snapshot,
                    workflow=cover_workflow,
                    is_cancelled=is_cancelled,
                    progress=cover_progress,
                )
                progress("完成", 1.0)
                return sub
            if subtitle_fallback == "fail":
                raise RuntimeError("平台没有可用字幕，且本次计划禁止本地转写回退")
            progress("没有可用的平台字幕,改用本地转写…", -1.0)

        with _transcription_tempdir() as tmp:
            tmp_dir = Path(tmp)

            # 1) 取得本地媒体文件(本地直通 or 链接下载)
            link_meta: dict = {}
            saved_media: str | None = None
            wav_path: Path | None = None

            # 主页批量转录先只读复用受管原文件。验证后文件若发生变化，
            # 仅在进入模型前回退一次；原文件永远不移动、不复制、不删除。
            if managed_input.valid and managed_input.path is not None:
                try:
                    progress("使用已下载音视频转录…", 0.05)
                    media_path = resolve_source(
                        str(managed_input.path),
                        tmp_dir,
                        enable_link_download,
                        input_progress,
                        link_meta,
                        link_media_mode="audio",
                    )
                    _check()
                    progress("转码音频(16k 单声道)…", 0.22)
                    stage_changed("conversion")
                    wav_path = to_wav_16k_mono(
                        media_path,
                        tmp_dir / "managed_audio_16k.wav",
                        is_cancelled=lambda: (_check() or False), timeout=600,
                    )
                    _check()
                except _Cancelled:
                    raise
                except (OSError, RuntimeError):
                    _check()
                    wav_path = None
                    link_meta = {}
                    progress("本地音视频不可用，改用链接临时下载…", -1.0)

            if wav_path is None:
                if (
                    execution_policy is not None
                    and execution_policy.origin == "homepage_batch_transcribe"
                    and not managed_input.valid
                ):
                    progress("没有可复用的本地素材，临时下载转录输入…", -1.0)
                progress("准备输入…", 0.05)
                media_path = None
                stage_changed("download")
                if (enable_link_download and execution_policy is not None
                        and execution_policy.origin == "homepage_batch_transcribe"):
                    from .collect.transcription_input import download_collected_input
                    def collected_progress(message, fraction):
                        _check()
                        input_progress(message, fraction)
                    try:
                        media_path = download_collected_input(
                            execution_policy.stable_key, tmp_dir, collected_progress, link_meta)
                    except _Cancelled:
                        raise
                    except (OSError, RuntimeError):
                        _check()
                        link_meta = {}
                        progress("已采集地址暂不可用，重新取得作品地址…", -1.0)
                if media_path is None:
                    media_path = resolve_source(
                        source,
                        tmp_dir,
                        enable_link_download,
                        input_progress,
                        link_meta,
                        link_media_mode=link_media_mode,
                        is_cancelled=lambda: (_check() or False),
                    )
                _check()
                link_meta = _settle_single_link_cover(
                    link_meta,
                    snapshot=cover_snapshot,
                    workflow=cover_workflow,
                    is_cancelled=is_cancelled,
                    progress=cover_progress,
                )

                # 1.5) 如勾选「保留下载的音视频」,且确为链接下载,则另存一份
                if keep_media and is_link and media_dir:
                    saved_media = _save_media(
                        media_path,
                        media_dir,
                        stage_callback(progress, 0.16, 0.22),
                    )

                # 2) 统一转码成 16k 单声道 wav
                progress("转码音频(16k 单声道)…", 0.22)
                stage_changed("conversion")
                wav_path = to_wav_16k_mono(
                    media_path,
                    tmp_dir / "audio_16k.wav",
                    is_cancelled=lambda: (_check() or False), timeout=600,
                )
                _check()

            input_lease.close()

            # 3) 引擎转录
            stage_changed("transcription")
            if required_format == 'srt':
                from .engines.local_models import LOCAL_MODELS
                from .engines.providers import DEFAULT_ENGINE
                selected = LOCAL_MODELS.get(engine_name or DEFAULT_ENGINE)
                if selected is not None and not selected.timestamps:
                    raise ValueError('没有可用平台字幕；所选模型不提供时间点，请选择 Whisper 或 MOSS 后重试。')
            engine = get_engine(engine_name)
            progress(f"使用引擎:{engine.display_name}", 0.32)
            transcribe_parameters = inspect.signature(engine.transcribe).parameters
            if "is_cancelled" in transcribe_parameters:
                transcript = engine.transcribe(
                    wav_path,
                    engine_progress,
                    is_cancelled=is_cancelled,
                )
            else:
                # Third-party/custom engines written against the older public
                # two-argument seam remain usable. Built-in engines all expose the
                # cooperative cancellation parameter.
                transcript = engine.transcribe(wav_path, engine_progress)
            _check()

            # 记录原始输入(而非临时 wav)+ 链接来源的真实标题
            if not (transcript.text or "").strip():
                raise RuntimeError("未识别到有效语音，请检查音频是否有声音，或换一个本地模型重试。")
            stage_changed("result")
            transcript.source = source
            transcript.saved_media = saved_media
            transcript.title = link_meta.get("title")
            transcript.meta = {**(transcript.meta or {}), **link_meta,
                               **({"asr": transcript.meta["asr"]} if "asr" in (transcript.meta or {}) else {})}
            progress("正在整理并保存转录结果…", 0.97)
            progress("完成", 1.0)
            return transcript


def _settle_single_link_cover(
    meta: object,
    *,
    snapshot: CollectCoverPreferenceSnapshot,
    workflow: SingleLinkCoverWorkflow | None,
    is_cancelled: Callable[[], bool],
    progress: ProgressCallback,
) -> dict:
    """结算单链接封面旁路；异常只写 meta，不改变转录主链。"""

    current = TranscriptCover.from_meta(meta)
    if not current.url:
        return TranscriptCover(
            platform=current.platform,
            work_id=current.work_id,
            status="not_requested",
        ).merge_meta(meta)
    if not snapshot.include_cover or workflow is None or not current.eligible:
        return TranscriptCover(
            platform=current.platform,
            work_id=current.work_id,
            url=current.url,
            status="url_only",
        ).merge_meta(meta)

    progress("正在安全保存封面…", -1.0)
    try:
        receipt = workflow.localize(
            platform=current.platform,
            work_id=current.work_id,
            url=current.url,
            is_cancelled=is_cancelled,
        )
    except Exception:
        receipt = None

    status = str(getattr(receipt, "status", "") or "")
    reason = str(getattr(receipt, "reason_code", "") or "")
    if status in {"downloaded", "recovered_previous"} and _valid_cover_receipt(
        current, receipt
    ):
        progress("封面已安全保存", -1.0)
        return TranscriptCover(
            platform=current.platform,
            work_id=current.work_id,
            url=current.url,
            status="downloaded",
            local_path=str(getattr(receipt, "relative_path", "") or ""),
        ).merge_meta(meta)
    if status == "failed" and reason == "cover_cancelled":
        progress("封面保存已取消，继续转写…", -1.0)
        return TranscriptCover(
            platform=current.platform,
            work_id=current.work_id,
            url=current.url,
            status="url_only",
        ).merge_meta(meta)

    stable_reason = (
        reason
        if status == "failed" and reason in COVER_FAILURE_CODES
        else "cover_commit_failed"
    )
    progress("封面保存失败，继续转写…", -1.0)
    return TranscriptCover(
        platform=current.platform,
        work_id=current.work_id,
        url=current.url,
        status="failed",
        error_code=stable_reason,
    ).merge_meta(meta)


def _valid_cover_receipt(
    current: TranscriptCover, receipt: object
) -> bool:
    relative_path = str(getattr(receipt, "relative_path", "") or "")
    accepted = TranscriptCover(
        platform=current.platform,
        work_id=current.work_id,
        url=current.url,
        status="downloaded",
        local_path=relative_path,
    )
    size = getattr(receipt, "size", None)
    sha256 = getattr(receipt, "sha256", None)
    mime_type = getattr(receipt, "mime_type", None)
    return bool(
        accepted.status == "downloaded"
        and not getattr(receipt, "reason_code", "")
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
        and isinstance(sha256, str)
        and _SHA256_RE.fullmatch(sha256)
        and isinstance(mime_type, str)
        and mime_type in _COVER_MIMES
    )


def _save_media(media_path: Path, media_dir: str, progress: ProgressCallback) -> str | None:
    """把下载到的原始音视频复制到用户指定目录,返回保存路径(失败不影响转录)。"""
    temp_path: Path | None = None
    try:
        dst_dir = Path(media_dir).expanduser()
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = _unique_path(dst_dir / media_path.name)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{dst.name}.",
            suffix=".part",
            dir=dst_dir,
        )
        os.close(fd)
        temp_path = Path(temp_name)
        shutil.copy2(media_path, temp_path)
        with temp_path.open("rb") as stream:
            os.fsync(stream.fileno())
        temp_path.replace(dst)
    except Exception as exc:  # noqa: BLE001
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        progress(f"保留音视频失败(不影响转录):{exc}", -1.0)
        return None
    progress(f"已保留音视频:{dst}", -1.0)
    return str(dst)


def _unique_path(dst: Path) -> Path:
    """若目标已存在,自动加 _1 / _2 …,避免覆盖。"""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


class _Cancelled(InterruptedError):
    """内部:用户取消。"""


def was_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, InterruptedError)
