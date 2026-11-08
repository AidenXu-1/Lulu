"""音色库:本地保存参考音频与参考文本。"""

from __future__ import annotations

import json
import errno
from contextlib import contextmanager
from functools import wraps
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from ..models_manager import app_data_dir
from .base import VoiceProfile

_VOICE_FIELDS = {f.name for f in fields(VoiceProfile)}


class VoiceLibraryError(ValueError):
    pass


class VoiceDeletionError(VoiceLibraryError):
    """Removal started but files or index still need cleanup."""


def _model_identity(engine):
    from .runtime_packages import TTS_RUNTIMES
    spec = TTS_RUNTIMES.get(engine)
    return spec.weights_repo if spec else ''


def _voice_from_dict(d: dict) -> VoiceProfile | None:
    """容错构造:按字段白名单过滤,缺 id/name 等必填或多余键都不至于整库崩。"""
    if not isinstance(d, dict):
        return None
    data = {k: v for k, v in d.items() if k in _VOICE_FIELDS}
    if (any(type(data.get(key)) is not str for key in ('id','name','reference_audio_path'))
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', data['id'])
            or not data['reference_audio_path']
            or any(key in data and type(data[key]) is not str for key in
                   ('reference_text','engine','model_id','created_at'))
            or 'meta' in data and type(data['meta']) is not dict):
        return None
    # Owned reference files move with their voice folder. Never retain a pointer
    # to the old library after a verified copy/switch; arbitrary external refs
    # retain their explicitly stored path.
    reference = Path(data['reference_audio_path'])
    if reference.is_absolute() and reference.parent.name == data['id'] and reference.name.startswith('reference.'):
        folder = VOICES_DIR / data['id']
        relocated = folder / reference.name
        # add_voice writes this sidecar with the original absolute reference.
        # Its matching origin survives a library copy; a coincidental filename
        # (even with an unrelated managed voice at the destination) does not.
        metadata = folder / 'meta.json'
        if reference != relocated and not folder.is_symlink() and not metadata.is_symlink() and metadata.is_file():
            try:
                origin = json.loads(metadata.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                origin = None
            if isinstance(origin, dict) and origin.get('id') == data['id'] and origin.get('reference_audio_path') == str(reference):
                data['reference_audio_path'] = str(relocated)
    engine = data.get('engine', 'local_voxcpm2')
    if not data.get('model_id') or data['model_id'] == 'openbmb/VoxCPM2' and engine != 'local_voxcpm2':
        data['model_id'] = _model_identity(engine)
    try:
        return VoiceProfile(**data)
    except TypeError:
        return None

VOICES_DIR = app_data_dir() / "voices"
INDEX = VOICES_DIR / "voices.json"
_mutation_lock = threading.RLock()


@contextmanager
def _voice_transaction():
    """GUI and Core serialize read/modify/write against the same voice directory."""
    import fcntl
    with _mutation_lock:
        _ensure()
        folder = os.open(VOICES_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open('.voices.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        finally:
            os.close(folder)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError('音色目录锁无法安全读取，原资料保留')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                from ..resource_control.contracts import resource_busy
                busy = resource_busy('VOICE_LIBRARY_BUSY')
                busy.args = ('音色库正在被另一个操作修改，请稍后重试；原音色保留',)
                raise busy from None
            yield
        finally:
            os.close(fd)


def _serialized_change(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _voice_transaction():
            return function(*args, **kwargs)
    return call


def _default_voices_dir() -> Path:
    return app_data_dir() / "voices"


def current_voice_library_dir() -> Path:
    return VOICES_DIR


def configure_voice_library_dir(path: str | Path | None) -> Path:
    global VOICES_DIR, INDEX
    with _mutation_lock:
        root = Path(path).expanduser() if path else _default_voices_dir()
        VOICES_DIR = root
        INDEX = VOICES_DIR / "voices.json"
        return VOICES_DIR


def _is_empty_dir(path: Path) -> bool:
    return not path.exists() or not any(path.iterdir())


def _assert_writable_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path, prefix='.lulu-voice-write-') as probe:
            probe.write(b'ok')
            probe.flush()
    except OSError as exc:
        raise ValueError("这个文件夹当前不可写，请换一个有权限的本机文件夹。") from exc


def relocate_voice_library(new_root: str | Path, *, progress=None, is_cancelled=None) -> Path:
    """切换音色库目录。

    不删除旧库；空目录会复制旧库，已有 voices.json 的目录视为现成 Lulu 音色库。
    """
    target = Path(new_root).expanduser()
    if not str(target).strip():
        raise ValueError("请选择一个本机文件夹作为音色库保存位置。")
    try:
        target_path, current_path = target.resolve(), VOICES_DIR.resolve()
        if target_path == current_path:
            _assert_writable_dir(target)
            return target
        if current_path in target_path.parents or target_path in current_path.parents:
            raise ValueError("请选择与当前音色库分开的文件夹。")
    except OSError:
        pass
    if target.exists() and not target.is_dir():
        raise ValueError("请选择文件夹，不要选择单个文件。")
    target_empty = _is_empty_dir(target)
    target_index = target / "voices.json"
    if not target_empty and not target_index.exists():
        raise ValueError("请选择空文件夹，或选择已有 Lulu 音色库文件夹。")
    if not target_empty:
        _read(strict=True, root=target)
    _assert_writable_dir(target)
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    if is_cancelled():
        raise RuntimeError("已取消")
    if VOICES_DIR.exists() and target_empty:
        with _voice_transaction(), tempfile.TemporaryDirectory(prefix=".lulu-voices-", dir=target) as scratch:
            _read(strict=True)
            stage = Path(scratch)
            items = [item for item in VOICES_DIR.iterdir() if item.name != ".voices.lock"]
            for index, item in enumerate(items):
                if is_cancelled():
                    raise RuntimeError("已取消")
                progress(f"正在复制音色资料（{index + 1} / {len(items)}）", index / len(items))
                if item.is_dir():
                    shutil.copytree(item, stage / item.name)
                else:
                    shutil.copy2(item, stage / item.name)
            if is_cancelled():
                raise RuntimeError("已取消")
            if any(item != stage for item in target.iterdir()):
                raise ValueError("目标文件夹已有新内容，请重新选择位置。")
            moved = []
            try:
                for item in stage.iterdir():
                    destination = target / item.name
                    item.rename(destination)
                    moved.append(destination)
            except OSError:
                for item in reversed(moved):
                    item.rename(stage / item.name)
                raise
    progress("已复制音色资料", 1.0)
    return configure_voice_library_dir(target)


def _ensure() -> None:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)


def _read(*, strict=False, root=None) -> list[dict]:
    folder = Path(root) if root is not None else VOICES_DIR
    index = folder/'voices.json' if root is not None else INDEX
    def invalid(message):
        if strict:
            raise VoiceLibraryError(message)
        return []
    if index.is_symlink():
        return invalid('音色库记录路径异常，原资料保留；请选择完整的音色库。')
    try:
        if not index.exists():
            if folder.exists() and any(child.is_dir() for child in folder.iterdir()):
                return invalid('音色库记录缺失，原参考音频保留；请恢复备份或选择完整的音色库。')
            return []
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return invalid('音色库记录暂时无法读取，原资料保留；请检查权限、恢复备份或选择完整的音色库。')
    if type(data) is not list:
        return invalid('音色库记录格式损坏，原资料保留；请恢复备份或选择完整的音色库。')
    if strict:
        rows = [_voice_from_dict(item) for item in data]
        if any(row is None for row in rows) or len({row.id for row in rows}) != len(rows):
            return invalid('部分音色记录无法完整核对，已暂停修改；原资料保留，请恢复备份或选择完整的音色库。')
    return data


def library_issue() -> str:
    try:
        _read(strict=True)
    except VoiceLibraryError as exc:
        return str(exc)
    return ''


def _write(items: list[dict]) -> None:
    _ensure()
    from ..library import _write_atomic_text
    _write_atomic_text(INDEX, json.dumps(items, ensure_ascii=False, indent=2))


def list_voices(*, strict=False) -> list[VoiceProfile]:
    return [v for v in (_voice_from_dict(d) for d in _read(strict=strict)) if v is not None]


def get_voice(voice_id: str) -> VoiceProfile | None:
    try:
        return next((v for v in list_voices(strict=True) if v.id == voice_id), None)
    except VoiceLibraryError:
        return None


@_serialized_change
def add_voice(name: str, reference_audio: str, reference_text: str = "", *, engine: str = "local_voxcpm2", meta: dict | None = None) -> VoiceProfile:
    items = _read(strict=True)
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
        model_id=_model_identity(engine),
        meta=meta or {},
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    items.insert(0, asdict(voice))
    _write(items)
    (vdir / "meta.json").write_text(
        json.dumps(asdict(voice), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return voice


@_serialized_change
def delete_voice(voice_id: str) -> None:
    if type(voice_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', voice_id):
        raise ValueError('音色编号无效，未删除任何资料。')
    items = [d for d in _read(strict=True) if d.get("id") != voice_id]
    folder = VOICES_DIR / voice_id
    if folder.is_symlink() or VOICES_DIR.is_symlink():
        raise VoiceLibraryError('音色文件夹路径已变化，未删除；请检查保存位置。')
    if folder.exists():
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            raise VoiceDeletionError('音色文件未能全部删除，列表记录已保留；请检查文件夹权限后重试。') from exc
    try:
        _write(items)
    except OSError as exc:
        raise VoiceDeletionError('音色文件已删除，但列表更新失败；请重试以完成列表清理。') from exc


def update_reference_text(voice_id: str, text: str) -> VoiceProfile:
    """Save corrected words without touching the reference recording."""
    if not text.strip():
        raise ValueError('参考文字不能为空。')
    return update_voice(voice_id, reference_text=text)


@_serialized_change
def update_voice(voice_id: str, *, name: str | None = None, reference_text: str | None = None) -> VoiceProfile:
    """Atomically edit metadata; leave the original audio and other fields intact."""
    from dataclasses import replace
    import os
    import tempfile
    if name is not None and not name.strip():
        raise ValueError('请填写音色名称。')
    items = _read(strict=True)
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
