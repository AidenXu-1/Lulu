"""音色库:本地保存参考音频与参考文本。"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from ..models_manager import app_data_dir
from .base import VoiceProfile

_VOICE_FIELDS = {f.name for f in fields(VoiceProfile)}


def _voice_from_dict(d: dict) -> VoiceProfile | None:
    """容错构造:按字段白名单过滤,缺 id/name 等必填或多余键都不至于整库崩。"""
    data = {k: v for k, v in d.items() if k in _VOICE_FIELDS}
    try:
        return VoiceProfile(**data)
    except TypeError:
        return None

VOICES_DIR = app_data_dir() / "voices"
INDEX = VOICES_DIR / "voices.json"


def _default_voices_dir() -> Path:
    return app_data_dir() / "voices"


def current_voice_library_dir() -> Path:
    return VOICES_DIR


def configure_voice_library_dir(path: str | Path | None) -> Path:
    global VOICES_DIR, INDEX
    root = Path(path).expanduser() if path else _default_voices_dir()
    VOICES_DIR = root
    INDEX = VOICES_DIR / "voices.json"
    return VOICES_DIR


def _is_empty_dir(path: Path) -> bool:
    return not path.exists() or not any(path.iterdir())


def _assert_writable_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".lulu_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError("这个文件夹当前不可写，请换一个有权限的本机文件夹。") from exc


def relocate_voice_library(new_root: str | Path) -> Path:
    """切换音色库目录。

    不删除旧库；空目录会复制旧库，已有 voices.json 的目录视为现成 Lulu 音色库。
    """
    target = Path(new_root).expanduser()
    if not str(target).strip():
        raise ValueError("请选择一个本机文件夹作为音色库保存位置。")
    try:
        if target.resolve() == VOICES_DIR.resolve():
            _assert_writable_dir(target)
            return target
    except OSError:
        pass
    if target.exists() and not target.is_dir():
        raise ValueError("请选择文件夹，不要选择单个文件。")
    target_empty = _is_empty_dir(target)
    target_index = target / "voices.json"
    if not target_empty and not target_index.exists():
        raise ValueError("请选择空文件夹，或选择已有 Lulu 音色库文件夹。")
    _assert_writable_dir(target)
    if VOICES_DIR.exists() and any(VOICES_DIR.iterdir()) and target_empty:
        for item in VOICES_DIR.iterdir():
            dst = target / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)
    return configure_voice_library_dir(target)


def _ensure() -> None:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)


def _read() -> list[dict]:
    if not INDEX.exists():
        return []
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write(items: list[dict]) -> None:
    _ensure()
    INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_voices() -> list[VoiceProfile]:
    return [v for v in (_voice_from_dict(d) for d in _read()) if v is not None]


def get_voice(voice_id: str) -> VoiceProfile | None:
    return next((v for v in list_voices() if v.id == voice_id), None)


def add_voice(name: str, reference_audio: str, reference_text: str = "", *, engine: str = "local_voxcpm2", meta: dict | None = None) -> VoiceProfile:
    src = Path(reference_audio).expanduser()
    if not src.exists():
        raise FileNotFoundError("参考音频不存在")
    voice_id = uuid.uuid4().hex[:12]
    vdir = VOICES_DIR / voice_id
    vdir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix or ".wav"
    dst = vdir / f"reference{suffix}"
    shutil.copy2(src, dst)
    voice = VoiceProfile(
        id=voice_id,
        name=name.strip() or f"音色 {datetime.now().strftime('%m-%d %H:%M')}",
        reference_audio_path=str(dst),
        reference_text=reference_text.strip(),
        engine=engine,
        meta=meta or {},
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    items = _read()
    items.insert(0, asdict(voice))
    _write(items)
    (vdir / "meta.json").write_text(
        json.dumps(asdict(voice), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return voice


def delete_voice(voice_id: str) -> None:
    items = [d for d in _read() if d.get("id") != voice_id]
    _write(items)
    shutil.rmtree(VOICES_DIR / voice_id, ignore_errors=True)


def update_reference_text(voice_id: str, text: str) -> VoiceProfile:
    """Save corrected words without touching the reference recording."""
    if not text.strip():
        raise ValueError('参考文字不能为空。')
    return update_voice(voice_id, reference_text=text)


def update_voice(voice_id: str, *, name: str | None = None, reference_text: str | None = None) -> VoiceProfile:
    """Atomically edit metadata; leave the original audio and other fields intact."""
    from dataclasses import replace
    import os
    import tempfile
    if name is not None and not name.strip():
        raise ValueError('请填写音色名称。')
    items = _read()
    original = next((item for item in items if item.get('id') == voice_id), None)
    voice = _voice_from_dict(original) if original else None
    if voice is None:
        raise ValueError('音色已不存在，请重新选择。')
    changes = {}
    if name is not None:
        changes['name'] = name.strip()
    if reference_text is not None:
        changes['reference_text'] = reference_text.strip()
    updated = replace(voice, **changes)
    items[items.index(original)] = {**original, **changes}
    fd, name = tempfile.mkstemp(prefix='.voices-', suffix='.json', dir=VOICES_DIR)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(items, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(INDEX)
    finally:
        temporary.unlink(missing_ok=True)
    return updated
