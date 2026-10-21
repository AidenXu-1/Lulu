"""本地模型的下载 / 存储 / 状态管理(统一受管目录)。

四模型各自使用受管目录 `<app-data>/Lulu/models/<engine_id>/`。
退役模型只保留历史文件和收据识别，不再下载或运行。
进度由调用方轮询 on_disk_size 估算(运行时无关、稳)。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import threading
from importlib import metadata, util
from pathlib import Path
import re

from .engines.local_models import LOCAL_MODELS, LOCAL_MODEL_FILES
from .engines.asr_resource import model_maintenance
from .artifact_trust import (
    capture_receipt_rebinds,
    install_receipt_valid,
    model_revision,
    modelscope_snapshot_download,
    rebind_install_receipts,
    remove_install_receipt,
    write_install_receipt,
    snapshot_install_receipt,
    restore_install_receipt,
)

_DONE = ".lulu_done"  # 受管模型下载完成标记


class DownloadCancelled(Exception):
    """模型下载被用户取消；已取得的候选缓存保持可继续。"""

# SenseVoice 需要主模型 + VAD 两个
_SENSEVOICE_SUBMODELS = {
    "SenseVoiceSmall": "iic/SenseVoiceSmall",
    "fsmn-vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
}

# Qwen3 想要时间戳,需额外的强制对齐模型(放 local_qwen3/_aligner 子目录)
_QWEN3_ALIGNER_REPO = "Qwen/Qwen3-ForcedAligner-0.6B"
_WHISPER_MLX_MIN_MACOS = (15, 0)
_MACOS_WHEEL_TAG = re.compile(r"macosx_(\d+)_(\d+)(?:_[^\s]+)?_arm64\Z")


def _app_data_dir() -> Path:
    home = Path.home()
    sysname = platform.system()
    if sysname == "Darwin":
        return home / "Library" / "Application Support" / "Lulu"
    if sysname == "Windows":
        import os
        return Path(os.environ.get("APPDATA", home)) / "Lulu"
    return home / ".local" / "share" / "Lulu"


def app_data_dir() -> Path:
    """App 的本地数据根目录(素材库 / 词典等始终在这,不随模型位置变)。"""
    return _app_data_dir()


def models_root() -> Path:
    """本地模型安装根目录:用户可改(config.models_dir);空=App 受管目录。
    ASR 在 <root>/models,TTS 运行时在 <root>/tts。"""
    try:
        from ..config import AppConfig   # 懒导入,避免循环
        d = (AppConfig.load().models_dir or "").strip()
        if d:
            return Path(d).expanduser()
    except Exception:  # noqa: BLE001
        pass
    return _app_data_dir()


def managed_dir() -> Path:
    return models_root() / "models"


def storage_dir() -> Path:
    return managed_dir()


def model_path(model_id: str) -> Path:
    return managed_dir() / model_id


@model_maintenance
def relocate_models(new_root, progress=None, is_cancelled=None, *, source_root=None) -> None:
    """把已安装的本地模型(models/ + tts/)从当前根移动到 new_root,并由调用方保存配置。
    同盘是快速 rename,跨盘是复制+删除(较慢)。失败抛异常、不动配置。"""
    progress = progress or (lambda *_a: None)
    is_cancelled = is_cancelled or (lambda: False)
    old_root = Path(source_root).expanduser() if source_root is not None else models_root()
    new_root = Path(new_root).expanduser()
    if new_root.resolve() == old_root.resolve():
        return
    old_root = old_root.resolve(strict=False)
    new_root.mkdir(parents=True, exist_ok=True)
    receipts = _receipt_base()
    captured_receipts = capture_receipt_rebinds(old_root, receipts)
    moved: list[str] = []
    try:
        for sub in ("models", "tts"):
            if is_cancelled():
                raise RuntimeError("已取消")
            src = old_root / sub
            if not src.exists():
                continue
            dst = new_root / sub
            if dst.exists():
                raise RuntimeError(f"新位置已存在 {sub} 目录,请换一个空目录,或先清理它。")
            progress(f"移动 {sub} 到新位置…", -1.0)
            shutil.move(str(src), str(dst))
            moved.append(sub)
        rebind_install_receipts(captured_receipts, new_root, receipts)
    except Exception:
        # 第二个目录失败时把已移动的第一个目录退回，避免 ASR/TTS 被拆在两处。
        for sub in reversed(moved):
            src = new_root / sub
            dst = old_root / sub
            if src.exists() and not dst.exists():
                old_root.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        try:
            rebind_install_receipts(captured_receipts, old_root, receipts)
        except Exception as rollback_exc:
            raise RuntimeError("模型搬家回滚后安装收据未能恢复，请保留现场检查。") from rollback_exc
        raise
    progress("已切换安装位置", 1.0)


@model_maintenance
def relocate_and_save(new_root, cfg, progress=None) -> None:
    """Commit the location only after the files and persistent configuration agree."""
    old_value = cfg.models_dir
    old_root = models_root().resolve()
    new_root = Path(new_root).expanduser().resolve()
    if old_root == new_root:
        return
    relocate_models(new_root, progress=progress, source_root=old_root)
    try:
        cfg.models_dir = str(new_root)
        if not cfg.save():
            raise RuntimeError("新位置未能保存")
    except Exception as exc:
        cfg.models_dir = old_value
        try:
            relocate_models(old_root, progress=progress, source_root=new_root)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"新位置未能保存，模型恢复原位置也未完成。请保留两处目录并重试：\n{old_root}\n{new_root}"
            ) from rollback_exc
        raise RuntimeError("新位置未能保存，模型已恢复原位置。请检查配置目录空间和权限后重试。") from exc


def _receipt_base() -> Path:
    return app_data_dir() / ".artifact-trust"


def _artifact_id(model_id: str) -> str:
    return f"asr-model:{model_id}"


def _trust_options(model_id: str) -> tuple[dict, ...]:
    if model_id in LOCAL_MODEL_FILES:
        return ({"source": "hf", "repositories": [
            {"repo": repo, "revision": model_revision(repo, "hf")}
            for repo in LOCAL_MODEL_FILES[model_id]], "file_set": 1},)
    if model_id == "local_sensevoice":
        return ({
            "source": "modelscope",
            "repositories": [
                {"repo": repo, "revision": model_revision(repo, "modelscope")}
                for repo in _SENSEVOICE_SUBMODELS.values()
            ],
        },)
    if model_id == "local_qwen3":
        primary = "Qwen/Qwen3-ASR-0.6B"
        return tuple({
            "source": "modelscope",
            "repositories": [
                {"repo": primary, "revision": model_revision(primary, "modelscope")},
                {"repo": _QWEN3_ALIGNER_REPO, "revision": model_revision(_QWEN3_ALIGNER_REPO, source)},
            ],
            "aligner_source": source,
        } for source in ("modelscope", "hf"))
    model = LOCAL_MODELS[model_id]
    source = model.repo_source
    return ({
        "source": source,
        "repositories": [{"repo": model.repo, "revision": model_revision(model.repo, source)}],
    },)


def installed_filename(model_id, name):
    # mlx-whisper expects this filename; only the name changes, never the bytes.
    return 'weights.safetensors' if model_id == 'local_whisper_mlx_4bit' and name == 'model.safetensors' else name


def verify_pinned_files(model_id, root, check=lambda: None):
    from .artifact_trust import _sha256_file
    for files in LOCAL_MODEL_FILES.get(model_id, {}).values():
        for name, (size, digest) in files.items():
            check()
            path = root / installed_filename(model_id, name)
            if (path.is_symlink() or not path.is_file() or path.stat().st_size != size
                    or _sha256_file(path, check) != digest):
                raise RuntimeError('模型文件与固定版本不符，原有模型已保留。')


def _shape_downloaded(model_id: str, root: Path) -> bool:
    if model_id in LOCAL_MODEL_FILES:
        try:
            return (root / _DONE).is_file() and all(
                not (root / installed_filename(model_id, name)).is_symlink()
                and (root / installed_filename(model_id, name)).stat().st_size == expected[0]
                for files in LOCAL_MODEL_FILES[model_id].values() for name, expected in files.items())
        except OSError:
            return False
    if model_id == "local_sensevoice":
        return (root / "SenseVoiceSmall" / "model.pt").is_file() and (root / "fsmn-vad").is_dir()
    if model_id == "local_qwen3":
        return (root / _DONE).is_file() and (root / "_aligner" / _DONE).is_file()
    return (root / _DONE).is_file()


def is_downloaded(model_id: str) -> bool:
    root = managed_dir() / model_id
    if not _shape_downloaded(model_id, root):
        return False
    return any(
        install_receipt_valid(_artifact_id(model_id), root, trust, _receipt_base())
        for trust in _trust_options(model_id)
    )


def needs_repair(model_id: str) -> bool:
    """旧版模型字节仍在，但缺少当前版本的可验证安装收据。

    这个状态只用于向用户解释“文件没丢，需要安全修复”，不会把未验证
    文件当成可执行模型。
    """
    root = managed_dir() / model_id
    return _shape_downloaded(model_id, root) and not is_downloaded(model_id)


def runtime_issue(model_id: str) -> str | None:
    """返回本地模型运行依赖问题;None 表示当前 App 运行时可尝试加载。

    受管模型文件下载完成不等于主程序运行依赖完整。Qwen3-ASR 的 transformers
    导入链会查询 torchcodec 的 package metadata;打包时若漏收 dist-info,用户会在
    转录弹窗里看到底层错误。这里把问题前置到就绪判定。
    """
    from .engines.local_models import RETIRED_MODELS
    if model_id in RETIRED_MODELS:
        return '此模型已退役，请选择 Q8、Whisper 或 MOSS；已有文件和历史文稿保留。'
    if model_id in {'local_sensevoice_q8', 'local_moss_q5'}:
        if platform.system() != 'Darwin' or platform.machine().lower() not in {'arm64', 'aarch64'}:
            return '此模型需要苹果芯 Mac。'
        try:
            if tuple(map(int, platform.mac_ver()[0].split('.')[:2])) < (15, 0):
                return '此模型需要 macOS 15 或更新版本。'
            from .engines.native_process import checked_tools
            checked_tools('moss' if model_id == 'local_moss_q5' else 'sensevoice-q8')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return '本地转录运行工具缺失或校验失败，请重新准备或更新 Lulu。'
        return None
    if model_id in {"local_whisper_mlx", "local_whisper_mlx_4bit"}:
        if platform.system() != "Darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
            return "Whisper MLX 只支持 Apple Silicon Mac；可继续使用 SenseVoice。"
        raw_version = platform.mac_ver()[0]
        try:
            current_macos = tuple(int(part) for part in raw_version.split(".")[:2])
        except (TypeError, ValueError):
            return "无法确认当前 macOS 版本，已停止使用 Whisper MLX；可继续使用 SenseVoice。"
        if len(current_macos) < 2:
            current_macos += (0,) * (2 - len(current_macos))
        if current_macos < _WHISPER_MLX_MIN_MACOS:
            return (
                f"Whisper MLX 需要 macOS 15 或更高版本，当前是 macOS {raw_version}；"
                "模型文件会保留，可继续使用 SenseVoice。"
            )
        try:
            wheel_text = metadata.distribution("mlx").read_text("WHEEL") or ""
        except Exception:  # noqa: BLE001 - 缺少运行时必须失败关闭
            return "Whisper MLX 运行依赖缺失，请更新 Lulu；模型文件会保留，可继续使用 SenseVoice。"
        wheel_minimums: list[tuple[int, int]] = []
        for line in wheel_text.splitlines():
            if not line.startswith("Tag:"):
                continue
            tag = line.partition(":")[2].strip()
            match = _MACOS_WHEEL_TAG.search(tag)
            if match:
                wheel_minimums.append((int(match.group(1)), int(match.group(2))))
        if not wheel_minimums:
            return (
                "Whisper MLX 运行依赖的系统兼容信息缺失，请更新 Lulu；"
                "模型文件会保留，可继续使用 SenseVoice。"
            )
        required_macos = min(wheel_minimums)
        if required_macos > current_macos:
            required_text = ".".join(str(part) for part in required_macos)
            return (
                f"当前 Lulu 的 Whisper MLX 需要 macOS {required_text}，当前是 macOS {raw_version}；"
                "模型文件会保留，可继续使用 SenseVoice。"
            )
        return None

    return None


def is_runtime_ready(model_id: str) -> bool:
    return runtime_issue(model_id) is None


def is_ready(model_id: str) -> bool:
    return is_downloaded(model_id) and is_runtime_ready(model_id)


def on_disk_size(model_id: str) -> int:
    p = model_path(model_id)
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _tree_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _resume_cache_dir(model_id: str) -> Path:
    trust = json.dumps(_trust_options(model_id), sort_keys=True, separators=(",", ":"))
    identity = hashlib.sha256(trust.encode("utf-8")).hexdigest()[:16]
    return managed_dir() / ".download-cache" / f"{model_id}-{identity}"


def resumable_download_size(model_id: str) -> int:
    return _tree_size(_resume_cache_dir(model_id) / "candidate")


def managed_storage_size() -> int:
    """返回 ASR 受管根的真实占用，包含已安装、待修复与断点缓存。"""
    return _tree_size(managed_dir())


def _private_candidate(model_id: str) -> tuple[Path, Path]:
    cache = _resume_cache_dir(model_id)
    parent = cache.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (parent, cache):
        if os.path.lexists(directory):
            info = directory.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError("模型下载恢复缓存不安全，已停止安装。")
        else:
            directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700, follow_symlinks=False)
    candidate = cache / "candidate"
    if os.path.lexists(candidate):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("模型下载候选目录不安全，已停止安装。")
    else:
        candidate.mkdir(mode=0o700)
    return cache, candidate


def _remove_managed_path(path: Path) -> None:
    """删除精确受管路径；符号链接只删链接本身，永不跟随外部目标。"""
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        path.unlink()
    else:
        shutil.rmtree(path)
    if os.path.lexists(path):
        raise OSError(f"未能完整删除 {path.name}")


@model_maintenance
def delete(model_id: str) -> None:
    if model_id not in LOCAL_MODELS:
        raise ValueError(f"未知本地模型：{model_id}")
    _remove_managed_path(model_path(model_id))
    cache_root = managed_dir() / ".download-cache"
    if cache_root.is_dir() and not cache_root.is_symlink():
        for child in cache_root.iterdir():
            if child.name.startswith(f"{model_id}-"):
                _remove_managed_path(child)
    remove_install_receipt(_artifact_id(model_id), _receipt_base())


@model_maintenance
def download_blocking(
    model_id: str,
    progress=None,
    is_cancelled=None,
) -> Path:
    """同步下载指定模型；固定版本候选可续传，成功校验后才原子提升。"""
    progress = progress or (lambda _fraction: None)
    is_cancelled = is_cancelled or (lambda: False)
    if is_cancelled():
        raise DownloadCancelled()
    if model_id in {"local_whisper_mlx", "local_whisper_mlx_4bit"}:
        issue = runtime_issue(model_id)
        if issue:
            raise RuntimeError(issue)
    m = LOCAL_MODELS[model_id]
    parent = managed_dir()
    parent.mkdir(parents=True, exist_ok=True)
    resume_cache, candidate = _private_candidate(model_id)
    selected_trust: dict
    expected = max(1, int(m.approx_size_mb * 1024 * 1024))
    transferred = resumable_download_size(model_id)
    progress_lock = threading.Lock()
    last_percent = -1
    class _SDKCancelled(BaseException):
        """Pass through SDK retry handlers without retrying a user pause."""

    def report(delta: int = 0) -> None:
        nonlocal transferred, last_percent
        if is_cancelled():
            raise _SDKCancelled()
        with progress_lock:
            transferred += max(0, int(delta or 0))
            fraction = min(0.99, transferred / expected)
            percent = int(fraction * 100)
            if percent != last_percent:
                last_percent = percent
                progress(fraction)

    # The desktop App never needs notebook auto-detection here.  The std backend
    # avoids importing optional interactive UI stacks inside a long-lived Qt process.
    from tqdm.std import tqdm as _Tqdm

    class _HFProgress(_Tqdm):
        def update(self, n=1):
            report(n)
            return super().update(n)

    def modelscope_progress_class():
        # Whisper uses Hugging Face only. Importing ModelScope there also imports
        # PyTorch into the GUI process, adding hundreds of MB and exposing native
        # library initialization conflicts after an earlier ASR engine ran.
        try:
            from modelscope.hub.callback import ProgressCallback as base
        except Exception:  # pragma: no cover - 测试假模块/缺少可选 ModelScope
            base = object

        class ModelScopeProgress(base):
            def __init__(self, filename: str, file_size: int):
                if base is not object:
                    super().__init__(filename, file_size)

            def update(self, size: int):
                report(size)

            def end(self):
                report(0)

        return ModelScopeProgress

    try:
        report(0)
        if model_id in LOCAL_MODEL_FILES:
            from .model_transfer import hf_snapshot
            for repo, files in LOCAL_MODEL_FILES[model_id].items():
                hf_snapshot(repo, revision=model_revision(repo, 'hf'), local_dir=str(candidate),
                            tqdm_class=_HFProgress, allow_patterns=list(files))
            if model_id == 'local_whisper_mlx_4bit':
                (candidate / 'model.safetensors').replace(candidate / 'weights.safetensors')
            verify_pinned_files(model_id, candidate, lambda: report(0))
            selected_trust = _trust_options(model_id)[0]
        else:  # hf
            from .model_transfer import hf_snapshot as snapshot_download
            snapshot_download(
                m.repo,
                revision=model_revision(m.repo, "hf"),
                local_dir=str(candidate),
                tqdm_class=_HFProgress,
            )
            selected_trust = _trust_options(model_id)[0]

        if is_cancelled():
            raise DownloadCancelled()
        (candidate / _DONE).write_text("ok", encoding="utf-8")
        if not _shape_downloaded(model_id, candidate):
            raise RuntimeError("模型下载不完整，关键文件缺失；原有模型已保留。")
        dest = _promote_candidate(model_id, candidate, selected_trust, is_cancelled)
        shutil.rmtree(resume_cache, ignore_errors=True)
        progress(1.0)
        return dest
    except _SDKCancelled as exc:
        raise DownloadCancelled() from exc


@model_maintenance
def _promote_candidate(model_id, candidate, trust, is_cancelled=lambda: False):
    parent = managed_dir()
    transaction = Path(tempfile.mkdtemp(prefix=f'.{model_id}-install-', dir=parent))
    backup, failed, dest = transaction/'previous', transaction/'failed', model_path(model_id)
    old_receipt = snapshot_install_receipt(_artifact_id(model_id), _receipt_base())
    backed_up = promoted = completed = False
    try:
        if is_cancelled(): raise DownloadCancelled()
        if os.path.lexists(dest):
            dest.replace(backup)
            backed_up = True
        candidate.replace(dest)
        promoted = True
        write_install_receipt(_artifact_id(model_id), dest, trust, _receipt_base())
        if not is_downloaded(model_id):
            raise RuntimeError('模型安装收据未能通过复核；原有模型已保留。')
        completed = True
        return dest
    except BaseException:
        if promoted and os.path.lexists(dest): dest.replace(failed)
        if backed_up and backup.exists() and not os.path.lexists(dest): backup.replace(dest)
        if failed.exists() and not os.path.lexists(candidate): failed.replace(candidate)
        restore_install_receipt(old_receipt)
        raise
    finally:
        if completed or not backup.exists(): shutil.rmtree(transaction, ignore_errors=True)


@model_maintenance
def import_local_model(model_id, source_root, *, is_cancelled=lambda: False):
    """Explicit import copies only pinned bytes; never moves the supplied source."""
    if model_id not in LOCAL_MODEL_FILES: raise ValueError('此模型不支持该导入方式')
    source_root = Path(source_root)
    if any(p.is_symlink() for p in (source_root, *source_root.parents)):
        raise ValueError('请选择原始模型文件夹，不使用符号链接')
    if source_root.resolve() == model_path(model_id).resolve():
        raise ValueError('源文件夹已是当前安装位置，请使用修复')
    cache, candidate = _private_candidate(model_id)
    for files in LOCAL_MODEL_FILES[model_id].values():
        for name in files:
            source = source_root/name
            if not source.exists(): source = source_root/installed_filename(model_id, name)
            if source.is_symlink() or not source.is_file(): raise ValueError('模型源文件缺失或路径无效')
            with source.open('rb') as inp, (candidate/installed_filename(model_id, name)).open('wb') as out:
                while data := inp.read(1024*1024):
                    if is_cancelled(): raise DownloadCancelled()
                    out.write(data)
    def check():
        if is_cancelled(): raise DownloadCancelled()
    verify_pinned_files(model_id, candidate, check)
    (candidate/_DONE).write_text('ok')
    dest = _promote_candidate(model_id, candidate, _trust_options(model_id)[0], is_cancelled)
    shutil.rmtree(cache, ignore_errors=True)
    return dest
