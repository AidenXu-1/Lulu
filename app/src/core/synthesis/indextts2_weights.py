"""IndexTTS2 主权重与辅助模型的下载 / 就绪判定。

官方主仓 `IndexTeam/IndexTTS-2.5` 自带新版 codec；不包含 W2V-BERT、
CAMPPlus 和 BigVGAN 的完整权重。Lulu 在安装期主动下载这些独立官方仓库，
并把所有 commit 一起写入安装收据，保证推理时完全离线。全部权重落进
checkpoints 目录（=运行时包的 weights 目录）。

主进程安全:本模块不 import 任何 TTS;huggingface_hub 仅在下载时懒导入。
"""

from __future__ import annotations

import os
import json
import shutil
import stat
import tempfile
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .. import models_manager as mm
from ..artifact_trust import (HF_MODEL_REVISIONS, install_receipt_valid, write_install_receipt,
                              snapshot_install_receipt, restore_install_receipt)

HF_REPO = "IndexTeam/IndexTTS-2.5"
HF_REVISION = HF_MODEL_REVISIONS[HF_REPO]
AUXILIARY_REPOSITORIES = {
    repo: HF_MODEL_REVISIONS[repo]
    for repo in (
        "facebook/w2v-bert-2.0",
        "funasr/campplus",
        "nvidia/bigvgan_v2_22khz_80band_256x",
    )
}
_W2V_REPO = "facebook/w2v-bert-2.0"
_CAMPPLUS_REPO = "funasr/campplus"
_BIGVGAN_REPO = "nvidia/bigvgan_v2_22khz_80band_256x"
_AUXILIARY_SINGLE_FILES = (
    (_CAMPPLUS_REPO, "campplus_cn_common.bin", "hf_cache/campplus_cn_common.bin"),
)

# 就绪判定:挑体量大/关键的文件 + 目录(齐了基本就是装好了);清单依据官方 cli_v2.py 的 REQUIRED_* 常量。
_REQUIRED_FILES = (
    "config.yaml",
    "gpt.pth",
    "s2mel.pth",
    "codec.pth",
    "multilingual_zh_ja_yue_char_del.tiktoken",
    "feat1.pt",
    "feat2.pt",
    "wav2vec2bert_stats.pt",
    "hf_cache/campplus_cn_common.bin",
    "hf_cache/bigvgan/config.json",
    "hf_cache/bigvgan/bigvgan_generator.pt",
)
_REQUIRED_DIRS = (
    "qwen0.6bemo4-merge",
    "hf_cache/w2v-bert-2.0",
)
# install 后强校验的「重头权重」——缺这些 = 下载不完整
_HEAVY = ("gpt.pth", "s2mel.pth", "qwen0.6bemo4-merge/model.safetensors",
          "hf_cache/w2v-bert-2.0/model.safetensors", "hf_cache/bigvgan/bigvgan_generator.pt")


@dataclass(frozen=True)
class WeightProfile:
    repo: str
    revision: str
    auxiliary: dict[str, str]
    single_files: tuple
    required_files: tuple[str, ...]
    receipt_id: str


def weight_profile(version: str = "2.5") -> WeightProfile:
    if version == "2.5":
        return WeightProfile(HF_REPO, HF_REVISION, AUXILIARY_REPOSITORIES,
                             _AUXILIARY_SINGLE_FILES, _REQUIRED_FILES,
                             "tts-weights:local_indextts2_5")
    if version != "2.0":
        raise ValueError(f"不支持的 IndexTTS 版本：{version}")
    repo = "IndexTeam/IndexTTS-2"
    auxiliary = {name: HF_MODEL_REVISIONS[name] for name in (
        _W2V_REPO, "amphion/MaskGCT", _CAMPPLUS_REPO, _BIGVGAN_REPO,
    )}
    files = tuple(f for f in _REQUIRED_FILES if f not in {
        "codec.pth", "multilingual_zh_ja_yue_char_del.tiktoken",
    }) + ("bpe.model", "hf_cache/semantic_codec_model.safetensors",
         "hf_cache/w2v-bert-2.0/config.json", "hf_cache/w2v-bert-2.0/preprocessor_config.json")
    single_files = (("amphion/MaskGCT", "semantic_codec/model.safetensors",
                     "hf_cache/semantic_codec_model.safetensors"), *_AUXILIARY_SINGLE_FILES)
    return WeightProfile(repo, HF_MODEL_REVISIONS[repo], auxiliary, single_files,
                         files, "tts-weights:local_indextts2")


@dataclass(frozen=True)
class WeightsStatus:
    installed: bool
    partial: bool
    missing_files: tuple[str, ...]
    existing_bytes: int


def checkpoints_dir(weights_root: Path) -> Path:
    """权重根目录(直接放 checkpoints 内容);weights_root 由 runtime_packages.weights_dir 给。"""
    return weights_root


def _existing_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def missing_files(weights_root: Path, *, version: str = "2.5") -> tuple[str, ...]:
    d = checkpoints_dir(weights_root)
    missing: list[str] = []
    for f in weight_profile(version).required_files:
        if not (d / f).exists():
            missing.append(f)
    for sub in _REQUIRED_DIRS:
        if not (d / sub).is_dir():
            missing.append(sub + "/")
    for f in _HEAVY:
        if f not in missing and not (d / f).exists():
            missing.append(f)
    return tuple(missing)


def _weights_trust(version: str = "2.5") -> dict:
    profile = weight_profile(version)
    return {
        "source": "hf",
        "repositories": [
            {"repo": profile.repo, "revision": profile.revision},
            *(
                {"repo": repo, "revision": revision}
                for repo, revision in profile.auxiliary.items()
            ),
        ],
    }


def _receipt_base(receipt_base: Path | None = None) -> Path:
    return receipt_base or mm.app_data_dir() / ".artifact-trust"


def _write_install_receipt(weights_root: Path, *, receipt_base: Path | None = None, version: str = "2.5") -> None:
    if missing_files(weights_root, version=version):
        raise RuntimeError("IndexTTS2 权重关键文件不完整，无法建立安装收据。")
    write_install_receipt(
        weight_profile(version).receipt_id,
        checkpoints_dir(weights_root),
        _weights_trust(version),
        _receipt_base(receipt_base),
    )


def status(weights_root: Path, *, receipt_base: Path | None = None, version: str = "2.5") -> WeightsStatus:
    d = checkpoints_dir(weights_root)
    missing = missing_files(weights_root, version=version)
    existing = _existing_bytes(d)
    trusted = not missing and install_receipt_valid(
        weight_profile(version).receipt_id, d, _weights_trust(version), _receipt_base(receipt_base)
    )
    return WeightsStatus(installed=trusted, partial=existing > 0 and not trusted,
                         missing_files=missing, existing_bytes=existing)


def is_installed(weights_root: Path, *, receipt_base: Path | None = None, version: str = "2.5") -> bool:
    return status(weights_root, receipt_base=receipt_base, version=version).installed


class _Cancelled(Exception):
    pass


def _resume_cache_dir(weights_root: Path, *, version: str = "2.5") -> Path:
    identity = hashlib.sha256(
        json.dumps(_weights_trust(version), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return weights_root.parent / f".indextts2-download-{weight_profile(version).revision[:8]}-{identity}"


def resumable_download_size(weights_root: Path, *, version: str = "2.5") -> int:
    """返回当前可恢复候选的真实字节数（包含辅助下载临时源）。"""
    cache = _resume_cache_dir(weights_root, version=version)
    legacy = weights_root.parent / f".indextts2-download-{weight_profile(version).revision[:16]}"
    return _existing_bytes(cache) + (0 if cache == legacy else _existing_bytes(legacy))


def _resume_candidate(weights_root: Path, *, version: str = "2.5") -> tuple[Path, Path]:
    cache = _resume_cache_dir(weights_root, version=version)
    legacy = weights_root.parent / f".indextts2-download-{weight_profile(version).revision[:16]}"
    if not os.path.lexists(cache) and os.path.lexists(legacy):
        legacy_info = legacy.lstat()
        if (
            stat.S_ISLNK(legacy_info.st_mode)
            or not stat.S_ISDIR(legacy_info.st_mode)
            or (hasattr(os, "getuid") and legacy_info.st_uid != os.getuid())
            or legacy_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("IndexTTS2 旧版下载恢复缓存不安全，已停止安装。")
        legacy.replace(cache)
    if os.path.lexists(cache):
        info = cache.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("IndexTTS2 下载恢复缓存不安全，已停止安装。")
    else:
        cache.mkdir(mode=0o700)
    os.chmod(cache, 0o700, follow_symlinks=False)
    candidate = cache / "candidate"
    if os.path.lexists(candidate):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("IndexTTS2 下载候选目录不安全，已停止安装。")
    else:
        candidate.mkdir(mode=0o700)
    return cache, candidate


def _incomplete_message(missing: tuple[str, ...]) -> str:
    shown = ", ".join(missing[:5])
    if len(missing) > 5:
        shown += f" 等 {len(missing)} 项"
    return (
        "模型权重下载不完整(缺 " + shown +
        "),请重试;若多次失败,请更换网络后继续安装,或先卸载该模型再重装。"
    )


def _copy_regular_file(source: Path, destination: Path) -> None:
    """把辅助仓库中的单文件原子平铺到 IndexTTS2 期望的布局。"""
    try:
        info = source.lstat()
    except OSError as exc:
        raise RuntimeError(f"IndexTTS2 辅助权重缺失：{source.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"IndexTTS2 辅助权重类型异常：{source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _download_auxiliary_models(candidate: Path, resume_cache: Path, snapshot_download, tqdm_class, *, version: str = "2.5") -> None:
    profile = weight_profile(version)
    w2v_dir = candidate / "hf_cache" / "w2v-bert-2.0"
    snapshot_download(
        _W2V_REPO,
        revision=profile.auxiliary[_W2V_REPO],
        local_dir=str(w2v_dir),
        tqdm_class=tqdm_class,
        **({"allow_patterns": ["config.json", "preprocessor_config.json", "model.safetensors"]}
           if version == "2.0" else {}),
    )

    source_root = resume_cache / "aux-source"
    for repo, remote_file, local_file in profile.single_files:
        repo_root = source_root / repo.replace("/", "--")
        snapshot_download(
            repo,
            revision=profile.auxiliary[repo],
            local_dir=str(repo_root),
            allow_patterns=[remote_file],
            tqdm_class=tqdm_class,
        )
        _copy_regular_file(repo_root / remote_file, candidate / local_file)

    bigvgan_dir = candidate / "hf_cache" / "bigvgan"
    snapshot_download(
        _BIGVGAN_REPO,
        revision=profile.auxiliary[_BIGVGAN_REPO],
        local_dir=str(bigvgan_dir),
        allow_patterns=["config.json", "bigvgan_generator.pt"],
        tqdm_class=tqdm_class,
    )


def install(
    weights_root: Path,
    progress,
    is_cancelled,
    *,
    _snapshot_download=None,
    _receipt_dir: Path | None = None,
    version: str = "2.5",
) -> None:
    """在隔离候选目录下载固定快照，成功前保留原权重。"""
    profile = weight_profile(version)
    d = checkpoints_dir(weights_root)
    receipt_base = _receipt_base(_receipt_dir)
    st = status(weights_root, receipt_base=receipt_base, version=version)
    if st.installed:
        progress("模型权重已就绪", 1.0)
        return
    if is_cancelled():
        raise _Cancelled()
    if st.partial:
        progress("检测到 IndexTTS2 权重未下完整,继续补齐缺失文件…", -1.0)
    else:
        progress("下载 IndexTTS2 模型权重(约 8G,较大,请耐心)…", -1.0)
    if _snapshot_download is None:
        from ..model_transfer import hf_snapshot as _snapshot_download
    parent = d.parent
    parent.mkdir(parents=True, exist_ok=True)
    resume_cache, candidate = _resume_candidate(weights_root, version=version)
    transferred = _existing_bytes(candidate)
    expected_bytes = int(8.3 * 1024**3)

    from tqdm.auto import tqdm as _Tqdm

    class _HFProgress(_Tqdm):
        def update(self, n=1):
            nonlocal transferred
            if is_cancelled():
                raise _Cancelled()
            transferred += max(0, int(n or 0))
            # Actual bytes and totals are reported by the shared installation session.
            from ..model_transfer import current
            if current() is None:
                progress("正在下载 IndexTTS2 模型文件…", -1)
            return super().update(n)

    promoted = False
    had_previous = os.path.lexists(d)
    completed = False
    old_receipt = snapshot_install_receipt(profile.receipt_id, receipt_base)
    try:
        _snapshot_download(
            profile.repo,
            revision=profile.revision,
            local_dir=str(candidate),
            tqdm_class=_HFProgress,
        )
        progress("下载 IndexTTS2 辅助模型…", -1.0)
        _download_auxiliary_models(candidate, resume_cache, _snapshot_download, _HFProgress, version=version)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, _Cancelled):
            raise
        raise RuntimeError(
            "IndexTTS2 权重下载未完成,已保留当前进度;请稍后重试或更换网络后继续安装。"
        ) from exc
    if is_cancelled():
        raise _Cancelled()
    missing = missing_files(candidate, version=version)
    if missing:
        raise RuntimeError(_incomplete_message(missing))
    transaction = Path(tempfile.mkdtemp(prefix=".indextts2-weights-install-", dir=parent))
    backup = transaction / "previous"
    failed = transaction / "failed"
    try:
        if had_previous:
            d.replace(backup)
        candidate.replace(d)
        promoted = True
        _write_install_receipt(d, receipt_base=receipt_base, version=version)
        if not is_installed(d, receipt_base=receipt_base, version=version):
            raise RuntimeError("IndexTTS2 安装收据未能通过复核；原有模型已保留。")
        completed = True
    except BaseException:
        if promoted and os.path.lexists(d):
            d.replace(failed)
        if had_previous and backup.exists() and not os.path.lexists(d):
            backup.replace(d)
        if failed.exists() and not os.path.lexists(candidate):
            candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            failed.replace(candidate)
        restore_install_receipt(old_receipt)
        raise
    finally:
        if completed or not backup.exists():
            shutil.rmtree(transaction, ignore_errors=True)
        if completed:
            shutil.rmtree(resume_cache, ignore_errors=True)
    progress("模型权重已就绪", 1.0)
