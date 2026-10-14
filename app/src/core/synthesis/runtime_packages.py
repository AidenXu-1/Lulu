"""本地 TTS「独立运行时包」的安装 / 卸载 / 状态(ADR-0015 + spec/tts-runtime-package-implementation)。

运行时包 = 一个可重定位的 Python(python-build-standalone)+ 该模型推理依赖,
我方按平台预构建、挂到 GitHub Releases(public 仓库 lulu-runtimes)。
本模块负责:从 Release 下载运行时包 + 解压到统一仓库 `<app-data>/tts/<id>/runtime`;
模型权重沿用现有 local_models(走官方 HF 缓存),不在运行时包内。

纯 stdlib(urllib/tarfile/hashlib),不 import 任何 TTS,主进程安全。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import multiprocessing
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import stat
from dataclasses import dataclass
from pathlib import Path

from .. import models_manager as mm
from ..artifact_trust import (
    HF_MODEL_REVISIONS,
    TTS_RUNTIME_ARTIFACTS,
    install_receipt_valid,
    remove_install_receipt,
    write_install_receipt,
)
from . import local_models
from . import install_support as support


class CancelledError(Exception):
    """安装被用户取消。"""


def _snapshot_download_child(
    repo: str,
    revision: str,
    cache_dir: str,
    connection,
    download_callable=None,
    allow_patterns=None,
) -> None:
    """Run one Hugging Face snapshot in a process the parent can reap."""
    try:
        if os.name != "nt":
            os.setsid()
        if download_callable is None:
            from huggingface_hub import snapshot_download
            from ..model_transfer import incremental_hf_transport
            with incremental_hf_transport():
                snapshot_download(repo, revision=revision, cache_dir=cache_dir,
                                  endpoint="https://huggingface.co", token=False,
                                  **({"allow_patterns": allow_patterns} if allow_patterns is not None else {}))
            receipt = ""
        else:
            receipt = download_callable(repo, revision, cache_dir)
        connection.send(("ok", receipt))
    except BaseException as exc:  # noqa: BLE001
        try:
            connection.send(("error", type(exc).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _stop_snapshot_process(process) -> None:
    if not process.is_alive():
        process.join(timeout=1)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM) if os.name != "nt" else process.terminate()
    except (OSError, ProcessLookupError):
        process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL) if os.name != "nt" else process.kill()
        except (OSError, ProcessLookupError):
            process.kill()
        process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError("模型权重下载进程无法安全停止")


def _snapshot_download_cancellable(
    repo: str,
    revision: str,
    cache_dir: Path,
    is_cancelled,
    *, allow_patterns=None,
) -> None:
    """Run snapshot_download without trapping cancellation behind its call."""
    from .. import model_transfer as transfer
    session = transfer.current()
    if session:
        plan = session.find(repo, revision, cache_dir)
        if plan is None:
            plan = session.add(transfer.hf_plan(repo, revision, cache_dir, local_dir=False,
                                                 allow_patterns=allow_patterns))
        if all(f.verified for f in plan.files):
            return
        with session.downloading():
            _snapshot_download_process(repo, revision, cache_dir, is_cancelled, allow_patterns=allow_patterns)
        plan.verify(session.check, session.report)
        return
    _snapshot_download_process(repo, revision, cache_dir, is_cancelled, allow_patterns=allow_patterns)


def _snapshot_download_process(repo, revision, cache_dir, is_cancelled, *, allow_patterns=None):
    module = sys.modules.get("huggingface_hub")
    if module is not None and getattr(module, "__spec__", None) is None:
        # Deterministic security/unit fakes live only in this process. Keep the
        # adapter narrow; real huggingface_hub always uses the process path.
        completed = threading.Event()
        outcome: list[BaseException | None] = []

        def invoke_fake() -> None:
            try:
                module.snapshot_download(repo, revision=revision, cache_dir=str(cache_dir), **({"allow_patterns": allow_patterns} if allow_patterns is not None else {}))
                outcome.append(None)
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)
            finally:
                completed.set()

        helper = threading.Thread(target=invoke_fake, name="lulu-hf-fixture")
        helper.start()
        while not completed.wait(0.01):
            if is_cancelled():
                raise CancelledError()
        helper.join()
        if outcome[0] is not None:
            raise outcome[0]
        return

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_snapshot_download_child,
        args=(repo, revision, str(cache_dir), child, None, allow_patterns),
        name="lulu-voxcpm2-download",
    )
    process.start()
    child.close()
    result: tuple[str, str] | None = None
    try:
        while process.is_alive():
            if parent.poll(0.05):
                result = parent.recv()
                break
            if is_cancelled():
                _stop_snapshot_process(process)
                raise CancelledError()
        process.join(timeout=5)
        if process.is_alive():
            _stop_snapshot_process(process)
        if result is None and parent.poll():
            result = parent.recv()
    finally:
        parent.close()
    if process.exitcode != 0 or result is None or result[0] != "ok":
        raise RuntimeError("模型权重下载失败")


@dataclass(frozen=True)
class TTSRuntimeSpec:
    model_id: str
    display_name: str
    releases_repo: str        # GitHub owner/repo,放运行时包 Release
    weights_repo: str         # 官方 HF 权重 repo(走 local_models 下载)
    approx_runtime_mb: int     # 运行环境包下载量(展示用)
    approx_weights_gb: float   # 权重下载量(展示用)
    hardware_note: str = ""
    # 「查看说明」弹窗字段(与 ASR ConfigDialog 同形,可直接复用)
    best_for: str = ""
    min_spec: str = ""
    features: str = ""
    platform_note: str = ""
    source: str = ""
    approx_size_mb: int = 0    # 总下载量(运行环境+权重),用于「占空间」一行
    timestamps: bool = False
    weights_handler: str = "voxcpm2"   # 权重下载/就绪策略：单仓库 VoxCPM2 或 IndexTTS
    storage_name: str = ""  # 保留逻辑引擎 ID/音色兼容，跨模型版本独立存储。
    runtime_download_available: bool = True
    min_spec_label: str = "最低配置"
    measured_spec: str = ""


# IndexTTS 2.5 保留 local_indextts2 引擎 ID，通过独立版本的受信任运行包下载。
# Phase3b 起配置页与生成链路同批接入 PBS 受管运行时:
# <pbs python> -m indextts.cli_v2 + vendor/index-tts PYTHONPATH + weights model-dir。
TTS_RUNTIMES: dict[str, TTSRuntimeSpec] = {
    "local_indextts2_0": TTSRuntimeSpec(
        model_id="local_indextts2_0", display_name="IndexTTS 2",
        releases_repo="AidenXu-1/lulu-runtimes", weights_repo="IndexTeam/IndexTTS-2",
        approx_runtime_mb=346, approx_weights_gb=8.3,
        hardware_note="本地 CPU 推理；首次加载较慢。",
        best_for="中英配音、声音克隆与情绪控制。",
        min_spec="已查官方说明未给出最低内存数值。Lulu 使用 Apple Silicon CPU；Mac 最低内存尚未验证。",
        min_spec_label="配置参考",
        measured_spec="2026-09-16，M4 Pro / CPU：40 字文案、12.85 秒参考音频，模型进程峰值 6.93 GiB；另测 120 字为 6.84 GiB。不含系统与其他程序，不能当作整机最低内存。",
        features="本地离线：声音克隆、选段试听、情感音频、文字描述和情绪向量。",
        platform_note="Apple Silicon · CPU", source='IndexTeam · <a href="https://huggingface.co/IndexTeam/IndexTTS-2">官方模型说明</a> · <a href="https://github.com/index-tts/index-tts">官方项目</a>',
        approx_size_mb=346 + int(8.3 * 1024), weights_handler="indextts2",
        storage_name="local_indextts2",
    ),
    "local_indextts2": TTSRuntimeSpec(
        model_id="local_indextts2",
        display_name="IndexTTS 2.5",
        releases_repo="AidenXu-1/lulu-runtimes",
        weights_repo="IndexTeam/IndexTTS-2.5",
        approx_runtime_mb=395,
        approx_weights_gb=9.9,
        hardware_note="本地 CPU 推理；首次加载较慢。",
        best_for="中英日西阿五语配音、声音克隆、读音和情绪控制。",
        min_spec="官方推理参考：NVIDIA 显卡，约 6 GB 显存。Lulu 使用 Apple Silicon CPU；显存不能直接换算成 Mac 内存，Mac 最低内存尚未验证。",
        min_spec_label="配置参考",
        measured_spec="2026-09-16，M4 Pro / CPU：40 字文案、12.85 秒参考音频，模型进程峰值 6.67 GiB。不含系统与其他程序，不能当作整机最低内存。",
        features="本地离线：五语、相对语速、读音标注、选段试听、情感音频/文本/8维向量。语速不保证精确时长；语言选择不翻译原文。",
        platform_note="Apple Silicon · CPU；Windows 后续支持",
        source='IndexTeam · <a href="https://huggingface.co/IndexTeam/IndexTTS-2.5">官方模型说明（含显存参考）</a> · 授权/商用需复核',
        approx_size_mb=10505,  # 实测权重 10,619,652,453 + 环境 395,001,800 字节，向上取 MiB。
        weights_handler="indextts2",
        storage_name="local_indextts2_5",
        runtime_download_available=True,  # 真实配音对照与公开附件摘要/下载验证通过。
    ),
    "local_voxcpm2": TTSRuntimeSpec(
        model_id="local_voxcpm2",
        display_name="VoxCPM2",
        releases_repo="AidenXu-1/lulu-runtimes",
        weights_repo="openbmb/VoxCPM2",
        approx_runtime_mb=330,
        approx_weights_gb=5.0,
        hardware_note="Apple Silicon 走 MPS;生成慢于实时,首次较慢。",
        best_for="给文案配音、克隆音色;中文自然。",
        min_spec="需要 Apple Silicon Mac；最低内存尚未验证，所需磁盘空间会在下载前检查。",
        min_spec_label="配置参考",
        features="零样本克隆:给一段参考音频即可仿音色;48kHz 高保真。生成慢于实时,首次加载约 2 分钟。",
        platform_note="Apple Silicon(MPS);Windows 后续支持",
        source="openbmb/VoxCPM2(Apache-2.0,可商用)",
        approx_size_mb=330 + 5 * 1024,
        weights_handler="voxcpm2",
    ),
    "local_qwen3_tts": TTSRuntimeSpec(
        model_id="local_qwen3_tts", display_name="Qwen3-TTS",
        releases_repo="AidenXu-1/lulu-runtimes", weights_repo="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        approx_runtime_mb=252, approx_weights_gb=13.6,
        hardware_note="Apple Silicon · 按需加载一种模式，支持释放内存",
        best_for="跨语言克隆、官方音色与文字设计声音。",
        min_spec="已在 48G 内存 Mac 验证，最低内存尚未验证；安装全部功能预留约 17G 硬盘。",
        min_spec_label="实测参考",
        features="三种模式、九种官方音色、表达控制；设计音色可保存后复用。语言选择不翻译原文。",
        platform_note="Apple Silicon · 官方 MPS BF16", source="Qwen/Qwen3-TTS · Apache-2.0",
        approx_size_mb=14000, weights_handler="qwen3", runtime_download_available=True,
    ),

}

_STATE = ".lulu_runtime.json"


def current_platform() -> str | None:
    """当前平台的运行时包标识;不支持的平台返回 None。"""
    mach = platform.machine().lower()
    if sys.platform == "darwin" and mach in ("arm64", "aarch64"):
        return "mac-arm64"
    if sys.platform.startswith("win"):
        return "win-cuda"
    return None


def tts_root() -> Path:
    # 与 ASR 共用「模型根目录」(用户可改);TTS 运行时在 <root>/tts
    return mm.models_root() / "tts"


def _runtime_cache_root() -> Path:
    return mm.app_data_dir() / "model-download-cache" / "tts-runtime"


def _runtime_cache_paths(model_id: str) -> list[Path]:
    root = _runtime_cache_root()
    if not os.path.lexists(root):
        return []
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("运行环境下载缓存身份异常，已停止操作。")
    if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("运行环境下载缓存权限异常，已停止操作。")
    return [child for child in root.iterdir() if child.name.startswith(f"{model_id}-")]


def model_dir(model_id: str) -> Path:
    spec = TTS_RUNTIMES.get(model_id)
    return tts_root() / (spec.storage_name if spec and spec.storage_name else model_id)


def _receipt_id(kind: str, model_id: str) -> str:
    spec = TTS_RUNTIMES[model_id]
    return f"{kind}:{spec.storage_name or model_id}"


def runtime_dir(model_id: str) -> Path:
    return model_dir(model_id) / "runtime"


def runtime_python(model_id: str) -> Path:
    """运行时包里解释器的绝对路径(主 App 子进程调它)。"""
    base = runtime_dir(model_id)
    if sys.platform.startswith("win"):
        return base / "python" / "python.exe"
    # PBS 解释器版本目录可能随构建变;取 bin 下第一个 python3.x
    binp = base / "python" / "bin"
    cand = sorted(binp.glob("python3.*")) if binp.is_dir() else []
    return cand[0] if cand else binp / "python3"


def _runtime_python_in(base: Path) -> Path:
    if sys.platform.startswith("win"):
        return base / "python" / "python.exe"
    binp = base / "python" / "bin"
    cand = sorted(binp.glob("python3.*")) if binp.is_dir() else []
    return cand[0] if cand else binp / "python3"


# 需 vendor 源码的模型:运行时包内 vendor/<子目录> 是源码根
_VENDOR_SUBDIR = {"local_indextts2": "index-tts", "local_indextts2_0": "index-tts"}


def vendor_dir(model_id: str) -> Path:
    """需 vendor 源码的模型(IndexTTS2):运行时包内的 vendor 源码根。"""
    return runtime_dir(model_id) / "vendor" / _VENDOR_SUBDIR[model_id]


def weights_dir(model_id: str) -> Path:
    """模型权重目录(VoxCPM2 走 local_models HF 缓存,不用此目录)。"""
    return model_dir(model_id) / "weights"


def _runtime_manifests(spec, plat):
    from ..artifact_trust import LEGACY_TTS_RUNTIME_ARTIFACTS
    manifests = [_trusted_manifest(spec, plat)]
    for artifact in LEGACY_TTS_RUNTIME_ARTIFACTS.get((spec.model_id, plat), ()):
        manifest = dict(model_id=artifact.model_id, platform=artifact.platform,
                        version=artifact.version, file=artifact.filename,
                        sha256=artifact.sha256, size=artifact.size)
        if manifest not in manifests:
            manifests.append(manifest)
    return manifests


def _installed_runtime_manifest(model_id: str) -> dict | None:
    try:
        spec = TTS_RUNTIMES[model_id]
        plat = current_platform()
        if plat is None:
            return None
        state = json.loads((model_dir(model_id) / _STATE).read_text(encoding="utf-8"))
        interpreter = runtime_python(model_id)
        if not interpreter.is_file() or interpreter.is_symlink():
            return None
        for manifest in _runtime_manifests(spec, plat):
            if state != _runtime_state(model_id, plat, manifest):
                continue
            health_failure = model_dir(model_id) / ".runtime-health-failure.json"
            if health_failure.exists():
                failed = json.loads(health_failure.read_text(encoding="utf-8"))
                info = interpreter.stat()
                if failed == {"version": manifest["version"], "device": info.st_dev, "inode": info.st_ino}:
                    return None
            if install_receipt_valid(
                _receipt_id("tts-runtime", model_id),
                runtime_dir(model_id),
                manifest,
                mm.app_data_dir() / ".artifact-trust",
            ):
                return manifest
        return None
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def is_runtime_installed(model_id: str) -> bool:
    return _installed_runtime_manifest(model_id) is not None


def _handler(model_id: str) -> str:
    spec = TTS_RUNTIMES.get(model_id)
    return spec.weights_handler if spec else "voxcpm2"


def index_weights_options(model_id: str) -> dict:
    """Keep legacy callers on 2.5; the separate 2.0 card selects its own assets."""
    return {"version": "2.0"} if model_id == "local_indextts2_0" else {}


def is_weights_installed(model_id: str) -> bool:
    h = _handler(model_id)
    if h == "qwen3":
        from . import qwen3_weights
        return qwen3_weights.is_installed(model_dir(model_id)/"models")
    if h == "indextts2":
        from . import indextts2_weights as iw
        return iw.is_installed(weights_dir(model_id), **index_weights_options(model_id))   # IndexTTS2 权重(checkpoints)放 weights 目录
    # VoxCPM2 权重沿用现有 local_models 缓存判定
    return local_models.is_downloaded(local_models.VOXCPM2.id)


def _directory_has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and next(path.iterdir(), None) is not None
    except OSError:
        return False


def _cache_path_has_data(path: Path) -> bool:
    """Ignore empty cache directories while surfacing unsafe identities for repair."""
    if not os.path.lexists(path):
        return False
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return True
        return next(path.iterdir(), None) is not None
    except OSError:
        return True


def has_existing_install_data(model_id: str) -> bool:
    """是否存在需要保留或修复的旧版/未完整安装数据。

    只做顶层目录检查，避免 UI 刷新时遍历数 GB 模型。
    """
    if _directory_has_entries(model_dir(model_id)):
        return True
    if any(_cache_path_has_data(path) for path in _runtime_cache_paths(model_id)):
        return True
    return _handler(model_id) == "voxcpm2" and _directory_has_entries(
        local_models.model_dir(local_models.VOXCPM2.id)
    )


def status(model_id: str) -> str:
    """not_installed / needs_repair / runtime_only / ready。"""
    rt = is_runtime_installed(model_id)
    wt = is_weights_installed(model_id)
    if rt and wt:
        return "ready"
    if rt or wt:
        return "runtime_only"
    if has_existing_install_data(model_id):
        return "needs_repair"
    return "not_installed"


def on_disk_size(model_id: str) -> int:
    total = 0
    base = model_dir(model_id)
    if _handler(model_id) == "qwen3":
        # Shared model files have several hard-linked paths, but occupy space once.
        return _tree_bytes(base) + sum(_tree_bytes(path) for path in _runtime_cache_paths(model_id))
    if base.exists():
        for f in base.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
    # VoxCPM2 权重在 local_models 缓存里(model_dir 之外),单独计入;
    if _handler(model_id) == "voxcpm2":
        total += local_models.on_disk_size(local_models.VOXCPM2.id)
    total += sum(_tree_bytes(path) for path in _runtime_cache_paths(model_id))
    return total


def _tree_bytes(path: Path) -> int:
    """Count physical bytes once per inode for progress and retained-size UI."""
    if not os.path.lexists(path):
        return 0
    try:
        root_info = path.lstat()
    except OSError:
        return 0
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    try:
        for candidate in path.rglob("*"):
            try:
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                info = candidate.stat()
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += getattr(info, "st_blocks", 0) * 512 or info.st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def partial_install_bytes(model_id: str) -> int:
    """Return safe, retryable bytes without treating them as a ready model."""
    value = support.partial_download_bytes(model_dir(model_id) / "downloads")
    value += sum(_tree_bytes(path) for path in _runtime_cache_paths(model_id))
    handler = _handler(model_id)
    if handler == "voxcpm2":
        root = local_models.model_dir(local_models.VOXCPM2.id)
        revision = HF_MODEL_REVISIONS[TTS_RUNTIMES[model_id].weights_repo]
        value += _tree_bytes(root / f".weights-download-{revision}")
    elif handler == "qwen3":
        value += _tree_bytes(model_dir(model_id)/"models")
    elif handler == "indextts2":
        from . import indextts2_weights as iw

        value += iw.resumable_download_size(weights_dir(model_id), **index_weights_options(model_id))
    else:
        value += _tree_bytes(vendor_dir(model_id))
    return value


def has_partial_install(model_id: str) -> bool:
    return status(model_id) == "runtime_only" or partial_install_bytes(model_id) > 0


# --------------------------------------------------------------------------- #
def _release_base(spec: TTSRuntimeSpec, plat: str) -> str:
    artifact_id = "local_indextts2" if spec.model_id == "local_indextts2_0" else spec.model_id
    tag = f"{artifact_id}-{plat}"
    return f"https://github.com/{spec.releases_repo}/releases/download/{tag}"


def _trusted_manifest(spec: TTSRuntimeSpec, plat: str) -> dict:
    """返回随 App 发布的可信锁，不从运行时下载自证清单。"""
    artifact = TTS_RUNTIME_ARTIFACTS.get((spec.model_id, plat))
    if artifact is None:
        raise RuntimeError("当前平台的运行环境尚未写入 App 可信锁，请更新 Lulu 后再试。")
    return {
        "model_id": artifact.model_id,
        "platform": artifact.platform,
        "version": artifact.version,
        "file": artifact.filename,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }


def _fetch_manifest(spec: TTSRuntimeSpec, plat: str) -> dict:
    """兼容安装适配层；真实值只能来自 App 内可信锁。"""
    return _trusted_manifest(spec, plat)


def _validate_manifest_lock(spec: TTSRuntimeSpec, plat: str, manifest: dict) -> dict:
    trusted = _trusted_manifest(spec, plat)
    if manifest != trusted:
        raise RuntimeError("运行环境清单与 App 可信锁不一致，已停止安装。")
    return trusted


def _runtime_state(model_id: str, plat: str, manifest: dict) -> dict:
    return {
        "schema": 1,
        "model_id": "local_indextts2" if model_id == "local_indextts2_0" else model_id,
        "platform": plat,
        "version": manifest.get("version", ""),
        "file": manifest["file"],
        "size": manifest["size"],
        "sha256": manifest["sha256"],
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


_RUNTIME_DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}

# GitHub Release 大文件在部分家庭网络/代理链路上会被中途断开。每次只请求一个
# 可独立复核的小范围，让故障半径从“整个 380MB 长连接”缩到单个 8MB 分片。
_RUNTIME_RANGE_BYTES = 8 * 1024 * 1024
_RUNTIME_RANGE_ATTEMPTS = 5


def _validate_runtime_url(url: str, *, redirect: bool) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("运行环境下载重定向地址格式异常，已停止安装。") from exc
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or literal is not None
        or host not in _RUNTIME_DOWNLOAD_HOSTS
    ):
        kind = "重定向" if redirect else "下载"
        raise RuntimeError(f"运行环境{kind}地址超出 App 可信域名，已停止安装。")


class _RuntimeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_runtime_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_runtime_url(url: str, timeout: int, headers: dict[str, str] | None = None):
    _validate_runtime_url(url, redirect=False)
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.build_opener(_RuntimeRedirectHandler()).open(request, timeout=timeout)


def _runtime_partial(path: Path, expected_size: int | None) -> tuple[Path, int]:
    partial = path.parent / f".{path.name}.lulu-resume"
    if not os.path.lexists(partial):
        return partial, 0
    fd = os.open(partial, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("运行环境下载恢复文件不安全，已停止安装。")
        if expected_size is not None and info.st_size > expected_size:
            os.close(fd)
            fd = -1
            partial.unlink()
            return partial, 0
        return partial, info.st_size
    finally:
        if fd >= 0:
            os.close(fd)


def _runtime_download_cache(model_id: str, sha256: str) -> Path:
    """Return a revision-bound private cache outside disposable install transactions."""
    base = mm.app_data_dir() / "model-download-cache"
    runtime_cache = _runtime_cache_root()
    cache = runtime_cache / f"{model_id}-{sha256[:16]}"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (base, runtime_cache, cache):
        if os.path.lexists(directory):
            info = directory.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError("运行环境下载缓存不安全，已停止安装。")
        else:
            directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700, follow_symlinks=False)
    return cache


def _existing_runtime_download_size(path: Path) -> int | None:
    if not os.path.lexists(path):
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("运行环境下载文件不安全，已停止安装。")
        return info.st_size
    finally:
        os.close(fd)


def _runtime_response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    return int(status or 200)


def _download(
    url: str,
    dest: Path,
    progress,
    is_cancelled,
    label: str,
    expected_size: int | None = None,
) -> None:
    if is_cancelled():
        raise CancelledError()
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing_size = _existing_runtime_download_size(dest)
    if existing_size is not None and (expected_size is None or existing_size == expected_size):
        progress(f"{label} 100%", 0.6)
        return
    if existing_size is not None:
        dest.unlink()
    tmp, done = _runtime_partial(dest, expected_size)
    if expected_size is not None and done == expected_size:
        os.replace(tmp, dest)
        progress(f"{label} 100%", 0.6)
        return

    def fetch_range(range_end: int | None):
        nonlocal done
        start = done
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": "Lulu-runtime-installer/1",
        }
        if expected_size is not None:
            assert range_end is not None
            headers["Range"] = f"bytes={start}-{range_end}"
        elif start:
            headers["Range"] = f"bytes={start}-"
        with _open_runtime_url(url, 60, headers=headers) as r:
            status = _runtime_response_status(r)
            length = int(r.headers.get("Content-Length") or 0)
            if expected_size is not None:
                # 可信大小已知时，即使从 0 开始也只接受精确 206 分片。若中间链路
                # 忽略 Range 返回 200，必须在打开断点文件前失败，绝不清零重下。
                content_range = r.headers.get("Content-Range") or ""
                match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
                expected_length = range_end - start + 1
                if (
                    status != 206
                    or match is None
                    or int(match.group(1)) != start
                    or int(match.group(2)) != range_end
                    or int(match.group(3)) != expected_size
                    or (length and length != expected_length)
                ):
                    raise support.InstallError(
                        "incomplete",
                        "下载分片响应不完整，已保留当前进度。",
                        retryable=True,
                    )
            elif start and status != 206:
                raise RuntimeError("运行环境续传响应异常，已停止安装。")
            elif not start and status != 200:
                raise RuntimeError("运行环境下载响应异常，已停止安装。")
            last = -1   # 上次上报的整百分比 / MB,避免每个 chunk 都刷信号
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_APPEND if start else os.O_TRUNC
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "ab" if start else "wb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != start:
                    raise RuntimeError("运行环境下载恢复文件已变化，已停止安装。")
                speed_bytes = start
                speed_at = time.monotonic()
                while True:
                    if is_cancelled():
                        raise CancelledError()
                    remaining = None if range_end is None else range_end + 1 - done
                    if remaining is not None and remaining <= 0:
                        break
                    chunk = r.read(min(1024 * 256, remaining) if remaining is not None else 1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if expected_size is not None and done > expected_size:
                        raise RuntimeError("下载文件超过 App 可信锁大小，已停止安装。")
                    total = expected_size or 0
                    if total:
                        pct = done * 100 // total
                        if pct != last:
                            last = pct
                            now = time.monotonic()
                            elapsed = max(0.001, now - speed_at)
                            speed = max(0, done - speed_bytes) / elapsed
                            progress(
                                f"{label} {pct}% · {support.format_bytes(done)} / "
                                f"{support.format_bytes(total)} · {support.format_bytes(int(speed))}/s",
                                0.05 + 0.55 * done / total,
                            )
                            speed_bytes = done
                            speed_at = now
                    else:
                        mb = done // 1048576
                        if mb != last:
                            last = mb
                            progress(f"{label} {mb}MB", -1.0)
                f.flush()
                os.fsync(f.fileno())
        if range_end is not None and done != range_end + 1:
            raise support.InstallError(
                "incomplete",
                "下载分片提前结束，已保留当前进度。",
                retryable=True,
            )
        if is_cancelled():
            raise CancelledError()

    try:
        while expected_size is None or done < expected_size:
            range_end = (
                min(expected_size - 1, done + _RUNTIME_RANGE_BYTES - 1)
                if expected_size is not None else None
            )
            support.retry_call(
                lambda _attempt: fetch_range(range_end),
                is_cancelled=is_cancelled,
                attempts=_RUNTIME_RANGE_ATTEMPTS,
                on_retry=lambda next_attempt, _code: progress(
                    f"{label}当前分片连接中断，正在自动续传（第 {next_attempt}/"
                    f"{_RUNTIME_RANGE_ATTEMPTS} 次）…",
                    -1.0,
                ),
                sleep=time.sleep,
            )
            if expected_size is None:
                break
        if expected_size is not None and done != expected_size:
            raise support.InstallError(
                "incomplete", "下载未达到可信大小，已保留当前进度。", retryable=True,
            )
        os.replace(tmp, dest)
    except support.CancelledError as exc:
        raise CancelledError() from exc
    except Exception as exc:  # noqa: BLE001
        code = support.normalise_error(exc)
        if code in {"network", "timeout", "http_5xx", "incomplete", "rate_limited"}:
            raise support.InstallError(
                code,
                "下载连接多次中断，已保留当前进度。请检查网络后点继续安装。",
            ) from exc
        raise


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


def _safe_extract_runtime(tar: tarfile.TarFile, dest: Path, is_cancelled) -> None:
    """Extract a runtime archive with traversal/link/device protection."""
    root = dest.resolve()
    members = tar.getmembers()
    if len(members) > 100_000:
        raise RuntimeError("运行环境包文件数量异常，已停止安装。")
    total_size = sum(member.size for member in members if member.isfile())
    if total_size > 12 * 1024**3 or any(member.size > 3 * 1024**3 for member in members):
        raise RuntimeError("运行环境包解压体积异常，已停止安装。")
    for member in members:
        if is_cancelled():
            raise CancelledError()
        name = member.name.replace("\\", "/")
        target = (root / name).resolve(strict=False)
        if name.startswith("/") or target != root and root not in target.parents:
            raise RuntimeError("运行环境包包含不安全路径,已停止安装。")
        if member.ischr() or member.isblk() or member.isfifo():
            raise RuntimeError("运行环境包包含不支持的特殊文件,已停止安装。")
        if member.issym() or member.islnk():
            link_base = target.parent if member.issym() else root
            link_target = (link_base / member.linkname).resolve(strict=False)
            if link_target != root and root not in link_target.parents:
                raise RuntimeError("运行环境包包含越界链接,已停止安装。")

    def _filter(member: tarfile.TarInfo, path: str):
        if is_cancelled():
            raise CancelledError()
        # macOS tar metadata is binary despite names like ._module.py. Some
        # dependencies scan all *.py; these sidecars must never become code.
        if any(part.startswith('._') for part in Path(member.name).parts):
            return None
        data_filter = getattr(tarfile, "data_filter", None)
        return data_filter(member, path) if data_filter is not None else member

    tar.extractall(dest, members=members, filter=_filter)


def check_runtime_health(model_id: str, root: Path, is_cancelled=lambda: False) -> None:
    """Import actual inference dependencies in a bounded, offline child process."""
    module = {"voxcpm2": "voxcpm", "indextts2": "indextts.infer_v2", "qwen3": "qwen_tts"}[_handler(model_id)]
    if model_id == "local_indextts2":
        module = "indextts.infer_v2_5"
    script = (
        "import socket\n"
        "def offline(*a, **k): raise RuntimeError('installation check is offline')\n"
        "socket.create_connection = offline\n"
        "socket.socket.connect = offline\n"
        "import importlib, torch\n"
        f"importlib.import_module({module!r})\n"
    )
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
               PYTHONDONTWRITEBYTECODE="1", TOKENIZERS_PARALLELISM="false")
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(root / "vendor/index-tts") if _handler(model_id) == "indextts2" else ""
    with tempfile.TemporaryDirectory(prefix="lulu-runtime-check-") as cache, tempfile.TemporaryFile() as log:
        env["NUMBA_CACHE_DIR"] = str(Path(cache) / "numba")
        env["MPLCONFIGDIR"] = str(Path(cache) / "matplotlib")
        env["XDG_CACHE_HOME"] = str(Path(cache) / "cache")
        child = subprocess.Popen([str(_runtime_python_in(root)), "-B", "-c", script],
                                 cwd=root, env=env, stdout=log, stderr=log,
                                 start_new_session=os.name != "nt")
        # Freshly extracted macOS dylibs can spend over 90 s in system signature
        # validation. Keep cancellation and a finite limit without rejecting a
        # healthy first import before that OS work has finished.
        deadline = time.monotonic() + 300
        try:
            while child.poll() is None:
                if is_cancelled():
                    raise CancelledError()
                if time.monotonic() >= deadline:
                    raise RuntimeError("运行环境检查超时，原模型已保留，请重试。")
                time.sleep(0.1)
            if child.returncode:
                raise RuntimeError("运行环境无法正常加载模型依赖，原模型已保留。请重新安装环境后重试。")
        finally:
            if child.poll() is None:
                if os.name == "nt":
                    child.kill()
                else:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


def install_runtime(model_id: str, progress=None, is_cancelled=None, *, local_archive: Path | None = None) -> None:
    """下载并解压运行时包到 `<app-data>/tts/<id>/runtime`。已装则跳过。
    progress(msg, frac);frac<0 表示不确定进度。"""
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    spec = TTS_RUNTIMES.get(model_id)
    if spec is None:
        raise ValueError(f"未知运行时:{model_id}")
    if is_cancelled():
        raise CancelledError()
    plat = current_platform()
    if plat is None:
        raise RuntimeError("当前平台暂不支持本地配音运行时(仅 Apple Silicon / Windows)。")
    if is_runtime_installed(model_id):
        progress("运行环境已就绪", 0.6)
        return
    if local_archive is None and not spec.runtime_download_available:
        raise RuntimeError("此模型的安装资源暂未上线，请稍后重试；现有模型和下载进度会保留。")

    progress("查询运行环境版本…", 0.02)
    manifest = _validate_manifest_lock(spec, plat, _fetch_manifest(spec, plat))
    fname = manifest["file"]
    want_sha = manifest["sha256"]
    want_size = manifest["size"]
    url = f"{_release_base(spec, plat)}/{fname}"

    rdir = runtime_dir(model_id)
    model_root = model_dir(model_id)
    model_root.mkdir(parents=True, exist_ok=True)
    download_cache = _runtime_download_cache(model_id, want_sha)
    transaction = Path(tempfile.mkdtemp(prefix=".runtime-install-", dir=model_root))
    staging = transaction / "candidate"
    backup = transaction / "previous-runtime"
    failed = transaction / "failed-runtime"
    state_file = model_root / _STATE
    state_backup = transaction / "previous-state.json"
    staging.mkdir(mode=0o700)
    archive = download_cache / fname
    had_runtime = os.path.lexists(rdir)
    had_state = state_file.exists()
    runtime_backed_up = False
    state_backed_up = False
    promoted = False
    completed = False
    from ..artifact_trust import snapshot_install_receipt, restore_install_receipt
    old_receipt = snapshot_install_receipt(_receipt_id("tts-runtime", model_id), mm.app_data_dir() / ".artifact-trust")
    try:
        if local_archive is None:
            from ..model_transfer import current
            from contextlib import nullcontext
            session = current()
            with session.downloading() if session else nullcontext():
                _download(url, archive, (lambda *_: None) if session else progress,
                          is_cancelled, "下载运行环境", want_size)
        else:
            # 先复制进私有事务目录，再验证并解压同一份内容；原文件不改、不消费。
            archive = transaction / "import.tar.gz"
            fd = os.open(local_archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != want_size:
                    raise RuntimeError("本地运行环境包大小不符，已停止安装。")
                with archive.open("wb") as target:
                    while chunk := source.read(1024 * 1024):
                        if is_cancelled():
                            raise CancelledError()
                        target.write(chunk)
        progress("校验完整性…", 0.62)
        if archive.stat().st_size != want_size or _sha256(archive) != want_sha:
            archive.unlink(missing_ok=True)
            raise RuntimeError("运行环境包校验失败(大小或 sha256 不符),请重试。")
        from ..model_transfer import current
        session = current()
        if session:
            plan = session.find(model_id, want_sha, download_cache)
            if plan:
                plan.files[0].verified = True
        progress("解压运行环境…", 0.65)
        with tarfile.open(archive, "r:gz") as tar:
            support.require_free_space(model_root, sum(m.size for m in tar.getmembers() if m.isfile()) + 256 * 1024**2)
            _safe_extract_runtime(tar, staging, is_cancelled)
        interpreter = _runtime_python_in(staging)
        if not interpreter.is_file() or interpreter.is_symlink():
            raise RuntimeError("运行环境解压后未找到可信解释器，安装失败。")
        progress("正在检查运行环境…", -1)
        check_runtime_health(model_id, staging, is_cancelled)
        if had_runtime:
            rdir.replace(backup)
            runtime_backed_up = True
        if had_state:
            state_file.replace(state_backup)
            state_backed_up = True
        staging.replace(rdir)
        promoted = True
        _write_json_atomic(state_file, _runtime_state(model_id, plat, manifest))
        write_install_receipt(
            _receipt_id("tts-runtime", model_id),
            rdir,
            manifest,
            mm.app_data_dir() / ".artifact-trust",
            inventory_paths=[path for path in rdir.rglob("*") if path.is_file() and not path.is_symlink()],
        )
        if not is_runtime_installed(model_id):
            raise RuntimeError("运行环境安装收据未能通过复核，安装失败。")
        completed = True
    except Exception:
        if promoted and os.path.lexists(rdir):
            rdir.replace(failed)
        if promoted:
            state_file.unlink(missing_ok=True)
        if runtime_backed_up and backup.exists() and not os.path.lexists(rdir):
            backup.replace(rdir)
        if state_backed_up and state_backup.exists() and not state_file.exists():
            state_backup.replace(state_file)
        restore_install_receipt(old_receipt)
        raise
    finally:
        if completed or not backup.exists() and not state_backup.exists():
            shutil.rmtree(transaction, ignore_errors=True)
        if completed:
            shutil.rmtree(download_cache, ignore_errors=True)
    progress("运行环境就绪", 0.7)


def install_weights(model_id: str, progress=None, is_cancelled=None) -> None:
    """下载模型权重(幂等,已就绪则跳过)。VoxCPM2 走单 HF 仓库。"""
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    spec = TTS_RUNTIMES.get(model_id)
    if spec is None:
        raise ValueError(f"未知运行时:{model_id}")
    if is_cancelled():
        raise CancelledError()
    if is_weights_installed(model_id):
        progress("模型权重已就绪", 0.98)
        return
    if spec.weights_handler == "qwen3":
        from . import qwen3_weights
        qwen3_weights.install(model_dir(model_id)/"models", progress, is_cancelled)
        return
    if spec.weights_handler == "indextts2":
        from . import indextts2_weights as iw
        try:
            iw.install(weights_dir(model_id), progress, is_cancelled, **index_weights_options(model_id))
        except iw._Cancelled as exc:
            raise CancelledError() from exc
        progress("模型权重就绪", 0.99)
        return
    from .weights_reuse import recover_voxcpm_weights
    def check_existing():
        if is_cancelled(): raise CancelledError()
    if recover_voxcpm_weights(check_existing, progress):
        progress("已有模型权重已校验并复用", .99)
        return
    progress("下载模型权重(较大,请耐心)…", -1.0)
    model_root = local_models.model_dir(local_models.VOXCPM2.id)
    model_root.mkdir(parents=True, exist_ok=True)
    cache = local_models.hf_cache_dir(local_models.VOXCPM2.id)
    attempt_root = Path(tempfile.mkdtemp(prefix=".weights-attempt-", dir=model_root))
    # Candidate belongs to the pinned revision and outlives this transaction.
    # Old installed files stay in hf-cache until the candidate passes verification.
    attempt_cache = model_root / f".weights-download-{HF_MODEL_REVISIONS[spec.weights_repo]}"
    if os.path.lexists(attempt_cache):
        info = attempt_cache.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_mode & 0o022
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise RuntimeError("模型续传目录不安全，已停止安装。")
    else:
        attempt_cache.mkdir(mode=0o700)
    previous_cache = attempt_root / "previous-hf-cache"
    promoted = False
    marker_created = False
    from ..artifact_trust import snapshot_install_receipt, restore_install_receipt
    old_receipt = snapshot_install_receipt(f"tts-weights:{model_id}", mm.app_data_dir() / ".artifact-trust")
    marker = model_root / local_models._DONE
    previous_marker = attempt_root / "previous-marker"
    operation_error: BaseException | None = None
    try:
        _snapshot_download_cancellable(
            spec.weights_repo,
            HF_MODEL_REVISIONS[spec.weights_repo],
            attempt_cache,
            is_cancelled,
        )
        if is_cancelled():
            raise CancelledError()
        if cache.exists():
            cache.replace(previous_cache)
        attempt_cache.replace(cache)
        promoted = True
        if is_cancelled():
            raise CancelledError()
        if marker.exists():
            marker.replace(previous_marker)
        local_models.mark_downloaded(local_models.VOXCPM2.id)
        marker_created = True
        if is_cancelled():
            raise CancelledError()
    except BaseException as exc:  # noqa: BLE001
        operation_error = exc
        if marker_created:
            (model_root / local_models._DONE).unlink(missing_ok=True)
        if promoted:
            try:
                cache.replace(attempt_cache)
            except OSError as rollback_exc:
                raise RuntimeError(
                    "模型权重回滚未闭合，已保留恢复现场"
                ) from rollback_exc
        if previous_cache.exists():
            if cache.exists():
                raise RuntimeError("模型权重回滚未闭合，已保留恢复现场")
            try:
                previous_cache.replace(cache)
            except OSError as rollback_exc:
                raise RuntimeError(
                    "模型权重回滚未闭合，已保留恢复现场"
                ) from rollback_exc
        if previous_marker.exists():
            marker.unlink(missing_ok=True)
            previous_marker.replace(marker)
        restore_install_receipt(old_receipt)
    try:
        shutil.rmtree(attempt_root)
    except OSError as cleanup_exc:
        raise RuntimeError("模型权重事务清理失败，旧缓存已保留") from cleanup_exc
    if operation_error is not None:
        raise operation_error
    progress("模型权重就绪", 0.99)


def install(model_id: str, progress=None, is_cancelled=None, *, local_archive: Path | None = None) -> None:
    """完整安装:运行环境(可取消)+ 模型权重。"""
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    if is_cancelled():
        raise CancelledError()
    # install_runtime 自己只事务化 runtime/状态/运行时收据；权重安装器也只
    # 事务化各自受管资产。这里不能再搬走整个模型根，否则运行时修复会让已下载
    # 权重、训练扩展和用户音色在安装期间不可见，并在成功后随备份一起被删除。
    if local_archive is None:
        install_runtime(model_id, progress, is_cancelled)
    else:
        install_runtime(model_id, progress, is_cancelled, local_archive=local_archive)
    if is_cancelled():
        raise CancelledError()
    install_weights(model_id, progress, is_cancelled)
    if is_cancelled():
        raise CancelledError()
    progress("安装完成", 1.0)


# --------------------------------------------------------------------------- #
def _remove_managed_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        path.unlink()
    else:
        shutil.rmtree(path)
    if os.path.lexists(path):
        raise OSError(f"未能完整删除 {path.name}")


def uninstall(model_id: str, *, remove_weights: bool = False) -> None:
    """卸载:删整个 tts/<id>/ 目录(含 runtime + vendor + weights)。
    VoxCPM2 权重在 model_dir 之外的 local_models 缓存,remove_weights 时另删。"""
    if model_id not in TTS_RUNTIMES:
        raise ValueError(f"未知本地配音模型：{model_id}")
    caches = _runtime_cache_paths(model_id)
    _remove_managed_path(model_dir(model_id))
    for cache in caches:
        _remove_managed_path(cache)
    receipts = mm.app_data_dir() / ".artifact-trust"
    remove_install_receipt(_receipt_id("tts-runtime", model_id), receipts)
    remove_install_receipt(_receipt_id("tts-weights", model_id), receipts)
    if _handler(model_id) == "qwen3":
        from .qwen3_model_lock import MODELS
        for name in MODELS:remove_install_receipt("qwen3-weights:"+name, receipts)
    if remove_weights and _handler(model_id) == "voxcpm2":
        _remove_managed_path(local_models.model_dir(local_models.VOXCPM2.id))
