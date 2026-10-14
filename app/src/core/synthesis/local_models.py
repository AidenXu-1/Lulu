"""本地 TTS 模型描述与状态。

与 ASR 的 `core.engines.local_models` 并行,但复用同一受管模型根目录。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import models_manager as mm
from ..artifact_trust import HF_MODEL_REVISIONS, install_receipt_valid, write_install_receipt


@dataclass(frozen=True)
class SynthesisModel:
    id: str
    display_name: str
    repo: str
    approx_size_mb: int
    speed_note: str


VOXCPM2 = SynthesisModel(
    id="local_voxcpm2",
    display_name="VoxCPM2 本地克隆音",
    repo="openbmb/VoxCPM2",
    approx_size_mb=10240,
    speed_note="占用视缓存与重复文件而定 · 建议预留 10-15GB · 首次加载约 2 分钟 · 生成慢于实时",
)

LOCAL_SYNTHESIS_MODELS = {VOXCPM2.id: VOXCPM2}
_DONE = ".lulu_done"


def _trust(model_id: str) -> dict:
    model = LOCAL_SYNTHESIS_MODELS[model_id]
    return {"hf_repo": model.repo, "hf_revision": HF_MODEL_REVISIONS[model.repo]}


def _receipt_base() -> Path:
    return mm.app_data_dir() / ".artifact-trust"


def _configured_root() -> Path:
    """返回克隆音模型根目录;优先环境变量,其次用户配置,最后 App 受管目录。"""
    env = os.environ.get("LULU_TTS_MODEL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from ...config import AppConfig
        cfg_dir = AppConfig.load().tts_model_dir.strip()
        if cfg_dir:
            return Path(cfg_dir).expanduser()
    except Exception:  # noqa: BLE001
        pass
    return mm.storage_dir()


def model_root() -> Path:
    return _configured_root()


def model_dir(model_id: str = VOXCPM2.id) -> Path:
    return model_root() / model_id


def hf_cache_dir(model_id: str = VOXCPM2.id) -> Path:
    return model_dir(model_id) / "hf-cache"


def mark_downloaded(model_id: str = VOXCPM2.id) -> None:
    root = model_dir(model_id)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _DONE
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{_DONE}.",
        suffix=".part",
        dir=root,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("ok")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(marker)
        # Transaction backups live beside hf-cache and are removed after commit.
        # Bind only the promoted payload, otherwise cleanup invalidates the receipt.
        inventory = [marker] + [
            path for path in hf_cache_dir(model_id).rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        write_install_receipt(
            f"tts-weights:{model_id}",
            root,
            _trust(model_id),
            _receipt_base(),
            inventory_paths=inventory,
        )
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        raise


def is_downloaded(model_id: str = VOXCPM2.id) -> bool:
    base = model_dir(model_id)
    has_payload = (base / _DONE).is_file() and any(
        hf_cache_dir(model_id).glob("models--openbmb--VoxCPM2/snapshots/*")
    )
    return has_payload and install_receipt_valid(
        f"tts-weights:{model_id}", base, _trust(model_id), _receipt_base()
    )


def on_disk_size(model_id: str = VOXCPM2.id) -> int:
    p = model_dir(model_id)
    if not p.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for f in p.rglob("*"):
        try:
            if f.is_file():
                st = f.stat()
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        except OSError:
            pass
    return total
