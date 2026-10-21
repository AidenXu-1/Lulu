"""App 内置的远端模型与可执行运行时信任锁。

这些值属于发布物的安全边界。上游更新不会被旧 App 自动接受；更新锁必须随新 App
发布并重新通过供应链回归。完整 commit revision 同时固定 Git/LFS 快照内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeArtifact:
    model_id: str
    platform: str
    version: str
    filename: str
    sha256: str
    size: int


TTS_RUNTIME_ARTIFACTS: dict[tuple[str, str], RuntimeArtifact] = {
    ('local_voxcpm2', "mac-arm64"): RuntimeArtifact(
        model_id='local_voxcpm2', platform="mac-arm64", version='20260920.1',
        filename='LuluTTSRuntime-local_voxcpm2-mac-arm64-20260920.1.tar.gz',
        sha256='1082a610fc0efd38071ef0c96f6246b76136ad2377042fb6e5a8411029ff77b6', size=329_991_474,
    ),
    ('local_qwen3_tts', "mac-arm64"): RuntimeArtifact(
        model_id='local_qwen3_tts', platform="mac-arm64", version='20260920.1',
        filename='LuluTTSRuntime-local_qwen3_tts-mac-arm64-20260920.1.tar.gz',
        sha256='4ce694e71bcd26523074713f718d7d79b414a1f239df84ce723ad5b52aab18e4', size=252_063_459,
    ),
    ('local_indextts2_0', "mac-arm64"): RuntimeArtifact(
        model_id='local_indextts2', platform="mac-arm64", version='20260920.3',
        filename='LuluTTSRuntime-local_indextts2-mac-arm64-20260920.3.tar.gz',
        sha256='d2c75862ae8bbbe9bb71123dfdc519d61194ebf1e76be1cca86b1b3e0cfb8689', size=346_317_334,
    ),
    ('local_indextts2', "mac-arm64"): RuntimeArtifact(
        model_id='local_indextts2', platform="mac-arm64", version='20260920.2',
        filename='LuluTTSRuntime-local_indextts2-mac-arm64-20260920.2.tar.gz',
        sha256='9a57896f74cb3a0202173cf167417c8511a8e376b6aaba57814edd4adaff2f97', size=395_001_800,
    ),
}

# Measured regular-file bytes; indexed by content digest, not a mutable version label.
TTS_RUNTIME_UNPACKED_BYTES = {
    '1082a610fc0efd38071ef0c96f6246b76136ad2377042fb6e5a8411029ff77b6': 1_186_125_007,
    '4ce694e71bcd26523074713f718d7d79b414a1f239df84ce723ad5b52aab18e4': 937_002_403,
    'd2c75862ae8bbbe9bb71123dfdc519d61194ebf1e76be1cca86b1b3e0cfb8689': 1_164_473_828,
    '9a57896f74cb3a0202173cf167417c8511a8e376b6aaba57814edd4adaff2f97': 1_450_468_749,
}

# Recognition only: new transfers must use TTS_RUNTIME_ARTIFACTS above.
LEGACY_TTS_RUNTIME_ARTIFACTS = {
    ("local_indextts2_0", "mac-arm64"): (RuntimeArtifact(
        model_id="local_indextts2", platform="mac-arm64", version="20260629",
        filename="LuluTTSRuntime-local_indextts2-mac-arm64-20260629.tar.gz",
        sha256="c95f1e0ceb726c8b844a6eabbbdad45dd6576067fab525d5d8bb017bbc3c12ca",
        size=397_816_510,
    ),),
    ("local_voxcpm2", "mac-arm64"): (RuntimeArtifact(
        model_id="local_voxcpm2", platform="mac-arm64", version="20260628",
        filename="LuluTTSRuntime-local_voxcpm2-mac-arm64-20260628.tar.gz",
        sha256="5ef14551adf9fe65518f1c288df433c398cc1fdf2d8e4fe667010ecf160f2bf3",
        size=366_012_874,
    ),),
    ("local_qwen3_tts", "mac-arm64"): (RuntimeArtifact(
        model_id="local_qwen3_tts", platform="mac-arm64", version="20260917.1",
        filename="LuluTTSRuntime-local_qwen3_tts-mac-arm64-20260917.1.tar.gz",
        sha256="cc920159732e7b25ba80310ac32bf4c143c691156ef7d73c47b924801681717e",
        size=272_124_163,
    ),),
    ("local_indextts2", "mac-arm64"): (RuntimeArtifact(
        model_id="local_indextts2", platform="mac-arm64", version="20260910.1",
        filename="LuluTTSRuntime-local_indextts2-mac-arm64-20260910.1.tar.gz",
        sha256="2ca5891aaa73fc25fa9bef22fa1f7d0ed6a4e05adbc22e36674af722279f7433",
        size=424_685_310,
    ), RuntimeArtifact(
        model_id="local_indextts2", platform="mac-arm64", version="20260920.1",
        filename="LuluTTSRuntime-local_indextts2-mac-arm64-20260920.1.tar.gz",
        sha256="135a7bfa8c0677de6459d1e1f101159cb6e5aae755661c2797557a04054db612",
        size=407_677_570,
    )),
}


HF_MODEL_REVISIONS: dict[str, str] = {
    "FunAudioLLM/SenseVoiceSmall-GGUF": "90c1c61912018b70ada0fcc024ea24aca62f2e63",
    "FunAudioLLM/fsmn-vad-GGUF": "6840bae4c5c92ee8c04faaf4db23dd0105098d7f",
    "mlx-community/whisper-large-v3-turbo-4bit": "0f058d38170d183f9fdee07908f5b515d91793a8",
    "handy-computer/moss-transcribe-diarize-gguf": "535085b2707309ceaa9802ae3078da03b8d2f244",
    "IndexTeam/IndexTTS-2.5": "c39ce5ba981572cb187443877ff559dfb246ce63",
    "IndexTeam/IndexTTS-2": "740dcaff396282ffb241903d150ac011cd4b1ede",
    "Qwen/Qwen3-ForcedAligner-0.6B": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7",
    "amphion/MaskGCT": "265c6cef07625665d0c28d2faafb1415562379dc",
    "facebook/w2v-bert-2.0": "da985ba0987f70aaeb84a80f2851cfac8c697a7b",
    "funasr/campplus": "e4b6ede7ce16997aff4ae69fbca1f0175e2afede",
    "mlx-community/whisper-large-v3-turbo": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
    "nvidia/bigvgan_v2_22khz_80band_256x": "633ff708ed5b74903e86ff1298cf4a98e921c513",
    "openbmb/VoxCPM2": "bffb3df5a29440629464e5e839f4d214c8714c3d",
}


MODELSCOPE_MODEL_REVISIONS: dict[str, str] = {
    "Qwen/Qwen3-ASR-0.6B": "4ce9cc728b473a5aedbe7b6e1ea45646316824dc",
    "Qwen/Qwen3-ForcedAligner-0.6B": "cf1c50164ea3ac48240d12bef5ead74aee0720cc",
    "iic/SenseVoiceSmall": "7bf452403abd7353a300cd760f7adae7701c92c1",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch": "f9a8b8274674755d925277e27063869038d41515",
}

_COMMIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODELSCOPE_HOSTS = frozenset({
    "modelscope.cn",
    "www.modelscope.cn",
    "modelscope.ai",
    "www.modelscope.ai",
})


def model_revision(repo: str, source: str) -> str:
    if source == "modelscope":
        locks = MODELSCOPE_MODEL_REVISIONS
    elif source == "hf":
        locks = HF_MODEL_REVISIONS
    else:
        raise RuntimeError(f"未知模型来源 {source}，已停止下载。")
    try:
        return locks[repo]
    except KeyError as exc:
        raise RuntimeError(f"模型 {repo} 尚未写入 App 可信版本锁，已停止下载。") from exc


def _trusted_modelscope_endpoint(endpoint: str) -> str:
    parsed = urlsplit((endpoint or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("ModelScope 下载端点格式异常，已停止下载。") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.hostname or "").lower() not in _MODELSCOPE_HOSTS
        or parsed.path not in {"", "/"}
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise RuntimeError("ModelScope 下载端点不在 App 官方可信范围，已停止下载。")
    return endpoint.rstrip("/")


def _trusted_modelscope_files(repo_files: Iterable[dict]) -> list[dict]:
    trusted: list[dict] = []
    seen: set[str] = set()
    for raw in repo_files:
        if not isinstance(raw, dict):
            raise RuntimeError("ModelScope 文件清单格式异常，已停止下载。")
        kind = raw.get("Type")
        if kind == "tree":
            continue
        if kind != "blob":
            raise RuntimeError("ModelScope 文件清单包含不支持的条目，已停止下载。")
        rel = raw.get("Path")
        sha256 = raw.get("Sha256")
        size = raw.get("Size")
        if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
            raise RuntimeError("ModelScope 文件清单路径异常，已停止下载。")
        path = PurePosixPath(rel)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RuntimeError("ModelScope 文件清单路径越界，已停止下载。")
        if rel in seen:
            raise RuntimeError("ModelScope 文件清单包含重复路径，已停止下载。")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError("ModelScope 文件清单大小异常，已停止下载。")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256.lower()) is None:
            raise RuntimeError("ModelScope 文件清单缺少可信 SHA-256，已停止下载。")
        clean = dict(raw)
        clean["Sha256"] = sha256.lower()
        trusted.append(clean)
        seen.add(rel)
    if not trusted:
        raise RuntimeError("ModelScope 固定版本没有可下载文件，已停止下载。")
    return trusted


def _sha256_file(path: Path, check=lambda: None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check()
            digest.update(chunk)
    return digest.hexdigest()


def modelscope_snapshot_download(
    repo: str,
    *,
    revision: str,
    local_dir: str,
    progress_callbacks=None,
) -> str:
    """按 App 内固定 commit 下载 ModelScope 快照并逐文件复核。"""
    expected = model_revision(repo, "modelscope")
    if revision != expected or _COMMIT_REVISION.fullmatch(revision) is None:
        raise RuntimeError(f"模型 {repo} 的 ModelScope commit 不在 App 可信锁中，已停止下载。")

    from .model_transfer import current
    session = current()
    plan = session.find(repo, revision, local_dir) if session else None
    if plan and all(f.verified for f in plan.files):
        return local_dir

    from modelscope.hub.api import HubApi, ModelScopeConfig
    from modelscope.hub.file_download import create_temporary_directory_and_cache
    from modelscope.hub.snapshot_download import _download_file_lists
    from modelscope.utils.constant import REPO_TYPE_MODEL

    api = HubApi()
    endpoint = _trusted_modelscope_endpoint(str(getattr(api, "endpoint", "")))
    cookies = api.get_cookies()
    headers = {
        "user-agent": ModelScopeConfig.get_user_agent(user_agent=None),
        "snapshot-identifier": str(uuid.uuid4()),
        "Snapshot": "True",
    }
    repo_files = _trusted_modelscope_files(api.get_model_files(
        model_id=repo,
        revision=revision,
        recursive=True,
        use_cookies=False if cookies is None else cookies,
        headers=headers,
        endpoint=endpoint,
    ))
    temporary_cache_dir, cache = create_temporary_directory_and_cache(
        repo,
        local_dir=local_dir,
        repo_type=REPO_TYPE_MODEL,
    )
    from contextlib import nullcontext
    from importlib import import_module
    ms_download = import_module("modelscope.hub.file_download")
    previous_parallel = getattr(ms_download, "MODELSCOPE_DOWNLOAD_PARALLELS", 1)
    # The single-file path appends actual received bytes to its resumable file.
    # Multi-file concurrency remains in the SDK snapshot downloader.
    ms_download.MODELSCOPE_DOWNLOAD_PARALLELS = 1
    try:
        with session.downloading() if session else nullcontext():
            _download_file_lists(
                repo_files,
                cache,
                temporary_cache_dir,
                repo,
                api,
                None,
                None,
                headers,
                repo_type=REPO_TYPE_MODEL,
                revision=revision,
                cookies=cookies,
                max_workers=8,
                endpoint=endpoint,
                progress_callbacks=progress_callbacks,
                pre_filtered=True,
            )
    finally:
        ms_download.MODELSCOPE_DOWNLOAD_PARALLELS = previous_parallel
    if plan:
        plan.verify(session.check, session.report)
    root = Path(local_dir)
    for meta in repo_files:
        path = root.joinpath(*PurePosixPath(meta["Path"]).parts)
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"ModelScope 模型文件缺失：{meta['Path']}") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"ModelScope 模型文件类型异常：{meta['Path']}")
        if info.st_size != meta["Size"] or _sha256_file(path) != meta["Sha256"]:
            raise RuntimeError(f"ModelScope 模型文件校验失败：{meta['Path']}")
    return str(root)


_RECEIPT_SCHEMA = 1


def _receipt_path(receipt_base: Path, artifact_id: str) -> Path:
    key = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
    return receipt_base / f"{key}.json"


def _mode(value: int) -> int:
    return stat.S_IMODE(value)


def _volume_uuid(path: Path) -> str:
    """Read macOS' persistent volume identity, not its per-mount device number.

    getattrlist/ATTR_VOL_UUID is a local filesystem query. No subprocess,
    network, or permission prompt; unsupported filesystems stay device-bound.
    """
    if sys.platform != 'darwin':
        return ''
    import ctypes
    import struct
    try:
        query = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).getattrlist
        query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_size_t, ctypes.c_ulong]
        query.restype = ctypes.c_int
        # struct attrlist: five bitmap groups; ATTR_VOL_INFO | ATTR_VOL_UUID.
        attrs = ctypes.create_string_buffer(struct.pack('=HHIIIII', 5, 0, 0, 0x80040000, 0, 0, 0))
        result = ctypes.create_string_buffer(20)
        if query(os.fsencode(path), attrs, result, len(result), 0) != 0:
            return ''
        if struct.unpack('=I', result.raw[:4])[0] != 20:
            return ''
        value = uuid.UUID(bytes=result.raw[4:20])
        return str(value) if value.int else ''
    except (OSError, AttributeError, ValueError):
        return ''


def _assert_private_entry(path: Path, *, directory: bool | None = None) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError("受管模型包含符号链接，无法建立可信安装收据。")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("受管模型根目录格式异常。")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise RuntimeError("受管模型包含不支持的特殊文件。")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("受管模型不属于当前用户，无法安全使用。")
    if os.name != "nt" and _mode(info.st_mode) & 0o022:
        raise RuntimeError("受管模型允许其他本机用户写入，无法安全使用。")
    return info


def _walk_private_tree(root: Path) -> list[Path]:
    _assert_private_entry(root, directory=True)
    files: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = _assert_private_entry(path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
                else:
                    raise RuntimeError("受管模型包含不支持的特殊文件。")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _file_entry(root: Path, path: Path) -> dict[str, int | str]:
    info = _assert_private_entry(path, directory=False)
    return {
        "path": path.relative_to(root).as_posix(),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "mode": _mode(info.st_mode),
    }


def _prepare_receipt_dir(receipt_base: Path) -> None:
    receipt_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = _assert_private_entry(receipt_base, directory=True)
    if os.name != "nt" and _mode(info.st_mode) != 0o700:
        receipt_base.chmod(0o700)
        _assert_private_entry(receipt_base, directory=True)


def write_install_receipt(
    artifact_id: str,
    root: Path,
    trust: dict,
    receipt_base: Path,
    *,
    inventory_paths: Iterable[Path] | None = None,
) -> Path:
    """在成功安装后写入私有收据；调用前会拒绝可被其他本机用户改写的树。"""
    requested_root = Path(os.path.abspath(root))
    root = requested_root.resolve(strict=True)
    if inventory_paths is None:
        selected = _walk_private_tree(root)
    else:
        _assert_private_entry(root, directory=True)
        selected = []
        for raw_path in inventory_paths:
            requested_path = Path(os.path.abspath(raw_path))
            try:
                relative = requested_path.relative_to(requested_root)
            except ValueError:
                raise RuntimeError("安装收据文件清单越过受管目录。")
            path = root / relative
            selected.append(path)
            current = path
            while current != root:
                _assert_private_entry(current)
                current = current.parent
    if not selected:
        raise RuntimeError("受管模型没有可绑定的文件，无法建立可信安装收据。")
    for path in selected:
        if path != root and root not in path.parents:
            raise RuntimeError("安装收据文件清单越过受管目录。")
    root_info = _assert_private_entry(root, directory=True)
    payload = {
        "schema": _RECEIPT_SCHEMA,
        "artifact_id": artifact_id,
        "root": str(root),
        "root_device": int(root_info.st_dev),
        "root_inode": int(root_info.st_ino),
        "trust": trust,
        "files": [_file_entry(root, path) for path in selected],
        "content_sha256": {path.relative_to(root).as_posix(): _sha256_file(path) for path in selected},
    }
    volume = _volume_uuid(root)
    if volume:
        payload['root_volume_uuid'] = volume
    _prepare_receipt_dir(receipt_base)
    target = _receipt_path(receipt_base, artifact_id)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=receipt_base)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        temp.replace(target)
        target.chmod(0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise
    return target


def install_receipt_valid(artifact_id: str, root: Path, trust: dict, receipt_base: Path) -> bool:
    """只接受由成功安装写出的、仍绑定同一目录和文件身份的收据。"""
    try:
        receipt = _receipt_path(receipt_base, artifact_id)
        receipt_info = _assert_private_entry(receipt, directory=False)
        if os.name != "nt" and _mode(receipt_info.st_mode) != 0o600:
            return False
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        resolved = root.resolve(strict=True)
        root_info = _assert_private_entry(resolved, directory=True)
        volume = payload.get('root_volume_uuid')
        stable_volume = isinstance(volume, str) and bool(volume)
        if stable_volume and _volume_uuid(resolved) != volume:
            return False
        if (
            payload.get("schema") != _RECEIPT_SCHEMA
            or payload.get("artifact_id") != artifact_id
            or payload.get("root") != str(resolved)
            or (not stable_volume and payload.get("root_device") != int(root_info.st_dev))
            or payload.get("root_inode") != int(root_info.st_ino)
            or payload.get("trust") != trust
        ):
            return False
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            return False
        for expected in files:
            rel = expected.get("path") if isinstance(expected, dict) else None
            if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
                return False
            path = resolved / rel
            current = _file_entry(resolved, path)
            if stable_volume:
                # Only normalize a device number for files on this same volume.
                if expected.get('device') != payload.get('root_device') or current['device'] != root_info.st_dev:
                    return False
                current['device'] = expected['device']
            if current != expected:
                return False
        return True
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return False


def remove_install_receipt(artifact_id: str, receipt_base: Path) -> None:
    _receipt_path(receipt_base, artifact_id).unlink(missing_ok=True)


def recover_device_receipt(artifact_id: str, root: Path, trust: dict, receipt_base: Path,
                           check=lambda: None) -> bool:
    """Recover only a device-number change, after checking every saved content hash.

    Legacy receipts without hashes remain strict; they need verification against
    the original source and must never be silently granted a weaker exception.
    """
    receipt = _receipt_path(receipt_base, artifact_id)
    try:
        _assert_private_entry(receipt, directory=False)
        payload = json.loads(receipt.read_text())
        resolved = root.resolve(strict=True)
        info = _assert_private_entry(resolved, directory=True)
        if (payload.get("schema") != _RECEIPT_SCHEMA or payload.get("artifact_id") != artifact_id
                or payload.get("root") != str(resolved) or payload.get("trust") != trust
                or payload.get("root_inode") != info.st_ino or payload.get("root_device") == info.st_dev):
            return False
        digests = payload.get("content_sha256", {})
        files = payload.get("files")
        if not isinstance(files, list) or not files or not isinstance(digests, dict):
            return False
        selected = []
        for expected in files:
            check()
            rel = expected.get("path")
            if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
                return False
            current = _file_entry(resolved, resolved / rel)
            if current["device"] != info.st_dev or any(
                current.get(key) != value for key, value in expected.items() if key != "device"
            ):
                return False
            if not isinstance(digests.get(rel), str) or _sha256_file(resolved / rel, check) != digests[rel]:
                return False
            selected.append(resolved / rel)
        # Writing rechecks private ownership and captures the new device identity.
        write_install_receipt(artifact_id, resolved, trust, receipt_base, inventory_paths=selected)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def snapshot_install_receipt(artifact_id: str, receipt_base: Path) -> tuple[Path, bytes | None]:
    path = _receipt_path(receipt_base, artifact_id)
    if not os.path.lexists(path):
        return path, None
    _assert_private_entry(path, directory=False)
    return path, path.read_bytes()


def restore_install_receipt(snapshot: tuple[Path, bytes | None]) -> None:
    path, data = snapshot
    if data is None:
        path.unlink(missing_ok=True)
        return
    _prepare_receipt_dir(path.parent)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".restore-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temp).replace(path)
    finally:
        Path(temp).unlink(missing_ok=True)


def install_receipt_present(artifact_id: str, receipt_base: Path) -> bool:
    """Return whether an artifact has ever entered the sealed-receipt regime."""
    try:
        return os.path.lexists(_receipt_path(receipt_base, artifact_id))
    except (OSError, ValueError, TypeError):
        return True


def capture_receipt_rebinds(source_root: Path, receipt_base: Path) -> list[dict]:
    """在模型搬家前捕获仍有效的收据；不接受失效或越界收据。"""
    if not source_root.exists() or source_root.is_symlink():
        return []
    source = source_root.resolve(strict=True)
    if not receipt_base.is_dir() or receipt_base.is_symlink():
        return []
    captured: list[dict] = []
    for receipt in receipt_base.glob("*.json"):
        try:
            info = _assert_private_entry(receipt, directory=False)
            if os.name != "nt" and _mode(info.st_mode) != 0o600:
                continue
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            artifact_root = Path(payload["root"]).resolve(strict=True)
            if artifact_root != source and source not in artifact_root.parents:
                continue
            artifact_id = payload["artifact_id"]
            trust = payload["trust"]
            if not install_receipt_valid(artifact_id, artifact_root, trust, receipt_base):
                continue
            captured.append({
                "artifact_id": artifact_id,
                "relative_root": artifact_root.relative_to(source).as_posix(),
                "trust": trust,
                "files": [entry["path"] for entry in payload["files"]],
            })
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return captured


def rebind_install_receipts(captured: Iterable[dict], target_root: Path, receipt_base: Path) -> None:
    """模型目录搬家成功后，把此前有效的收据绑定到新目录身份。"""
    target = target_root.resolve(strict=True)
    for item in captured:
        root = target / item["relative_root"]
        files = [root / relative for relative in item["files"]]
        write_install_receipt(
            item["artifact_id"],
            root,
            item["trust"],
            receipt_base,
            inventory_paths=files,
        )
