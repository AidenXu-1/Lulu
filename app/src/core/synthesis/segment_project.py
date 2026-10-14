"""Recoverable paragraph production, independent of model-internal alignment."""
from __future__ import annotations

import array
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import wave

from .base import SynthesisRequest, VoiceProfile

MAX_PARAGRAPHS = 50
MAX_PARAGRAPH_CHARS = 600
OPTION_KEYS = {
    'emotion_mode', 'emotion_preset', 'emotion_intensity', 'emotion_weight',
    'emotion_vector', 'emotion_text', 'emotion_audio_path', 'emotion_summary',
    'emotion_source_preset', 'emotion_source_intensity', 'style_preset',
    'language', 'speed', 'random_emotion', 'generation_options',
}


class ProjectCancelled(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def validate_text(text):
    text = str(text).strip()
    if not text:
        raise ValueError('这一段还没有文案。')
    if len(text) > MAX_PARAGRAPH_CHARS:
        raise ValueError('每段最多 600 字，请按完整意思拆成几段，用空行隔开。')
    return text


def paragraphs(text):
    import re
    parts = [validate_text(s) for s in re.split(r'\n\s*\n', str(text).strip()) if s.strip()]
    if not parts or len(parts) > MAX_PARAGRAPHS:
        raise ValueError('请准备 1～50 段文案，用空行隔开。')
    return parts


def inspect_wav(path):
    """Bounded scan: measured silence/clipping hints, never quality acceptance."""
    with wave.open(str(path), 'rb') as f:
        if f.getsampwidth() != 2 or f.getcomptype() != 'NONE' or f.getnchannels() not in (1, 2):
            raise ValueError('仅支持未压缩的 16 位 WAV。')
        rate, channels, frames = f.getframerate(), f.getnchannels(), f.getnframes()
        if rate <= 0 or frames <= 0:
            raise ValueError('音频为空，未保存为可用版本。')
        block_frames = max(1, rate // 100)
        windows, peak, full, count = [], 0, 0, 0
        while raw := f.readframes(block_frames):
            if len(raw) % (channels * 2):
                raise ValueError('音频数据不完整。')
            values = array.array('h', raw)
            if __import__('sys').byteorder != 'little':
                values.byteswap()
            peak = max(peak, max(abs(v) for v in values))
            full += sum(abs(v) >= 32767 for v in values)
            n = len(values) // channels
            windows.append((count / rate, (count + n) / rate,
                            math.sqrt(sum(v * v for v in values) / len(values))))
            count += n
        if count != frames:
            raise ValueError('音频数据不完整。')
    if peak == 0:
        raise ValueError('音频没有声音，请重新生成。')
    # Relative threshold also handles unusually quiet references/outputs.
    threshold = max(1, min(58, peak * .002))
    warnings, start, end = [], None, None
    for left, right, rms in windows + [(frames / rate, frames / rate, threshold + 1)]:
        if rms <= threshold:
            if start is None:
                start = left
            end = right
        elif start is not None:
            if end - start >= 1.2:
                warnings.append({'kind': 'silence', 'start': round(start, 2), 'end': round(end, 2),
                                 'message': f'{start:.1f}～{end:.1f} 秒声音很轻或有停顿，请试听'})
            start = end = None
    if full:
        warnings.append({'kind': 'full_scale', 'message': '检测到满幅采样，请试听是否有破音'})
    return {'frames': frames, 'rate': rate, 'channels': channels, 'duration': frames / rate,
            'full_scale_samples': full, 'warnings': warnings}


def reference_info(path):
    import soundfile as sf
    with sf.SoundFile(str(path)) as f:
        duration = len(f) / f.samplerate
        audio = f.read(min(len(f), int(15 * f.samplerate)), dtype='float32', always_2d=True)
    import numpy as np
    if duration < .5 or not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio)) == 0:
        raise ValueError('参考音频太短或没有有效声音，请重新选一段。')
    return {'duration': duration, 'used_seconds': min(duration, 15),
            'hint': '模型只读取前 15 秒，请确认这段完整、清楚。' if duration > 15 else '参考按原始音量保存。'}


class SegmentProject:
    def __init__(self, path):
        path = Path(path).expanduser().resolve()
        self.path = path if path.name == 'project.json' else path / 'project.json'
        self.root = self.path.parent
        self.read()

    def file(self, relative):
        value = Path(str(relative))
        if value.is_absolute() or '..' in value.parts:
            raise ValueError('项目文件路径无效。')
        path = self.root / value
        if path.resolve().parent != self.root.resolve() and self.root.resolve() not in path.resolve().parents:
            raise ValueError('项目文件路径无效。')
        if any(p.is_symlink() for p in [path, *path.parents] if p != self.root.parent):
            raise ValueError('项目文件不能使用链接。')
        return path

    def read(self):
        if self.path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('项目记录过大。')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or data.get('model_version') != '2.5':
            raise ValueError('请选择 Lulu IndexTTS 2.5 分段项目。')
        if not 1 <= len(data['parts']) <= MAX_PARAGRAPHS:
            raise ValueError('项目段落记录无效。')
        if set(data['options']) - OPTION_KEYS:
            raise ValueError('项目生成设置无效。')
        for part in data['parts']:
            validate_text(part['text'])
        return data

    @contextmanager
    def locked(self):
        with (self.root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, data, expected_revision):
        with self.locked():
            if self.read()['revision'] != expected_revision:
                raise ValueError('项目已在别处更新，请重新打开。已有声音已保留。')
            data = deepcopy(data)
            data['revision'] = expected_revision + 1
            self._write(self.path, data)

    @staticmethod
    def _write(path, data):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
                tmp = Path(f.name)
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if tmp and tmp.exists():
                tmp.unlink()

    @classmethod
    def create(cls, parent, text, voice, options):
        parts = paragraphs(text)
        if set(options) - OPTION_KEYS:
            raise ValueError('生成设置包含不支持的项目。')
        options = deepcopy(options)
        from .indextts2_worker import INDEX25_GENERATION_FIELDS, validate_index_generation
        overrides = validate_index_generation(options.get('generation_options', {}), '2.5')
        options['generation_options'] = {k: v[0] for k, v in INDEX25_GENERATION_FIELDS.items()}
        options['generation_options'].update(overrides)
        root = Path(parent).expanduser() / ('配音项目_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
        root.mkdir(parents=True, exist_ok=False)
        (root / 'references').mkdir()
        (root / 'versions').mkdir()
        (root / 'exports').mkdir()
        refs = {}
        try:
            for name, source in [('voice', voice.reference_audio_path), ('emotion', options.get('emotion_audio_path'))]:
                if not source:
                    continue
                source = Path(source).expanduser()
                if not source.is_file():
                    raise ValueError('参考音频不存在。')
                relative = 'references/' + name + source.suffix.lower()
                shutil.copyfile(source, root / relative)
                info = reference_info(root / relative)
                refs[name] = {'path': relative, 'sha256': digest(root / relative), **info}
            if 'voice' not in refs:
                raise ValueError('请选择参考音色。')
            if 'emotion' in refs:
                options['emotion_audio_path'] = refs['emotion']['path']
            profile = asdict(voice)
            profile['reference_audio_path'] = refs['voice']['path']
            data = {'schema': 1, 'revision': 0, 'model_version': '2.5', 'created': datetime.now().isoformat(),
                    'voice': profile, 'options': options, 'references': refs, 'gap_seconds': .2,
                    'parts': [{'id': uuid.uuid4().hex, 'text': s, 'selected': None, 'versions': []} for s in parts]}
            cls._write(root / 'project.json', data)
        except Exception:
            shutil.rmtree(root)  # only the new, not-yet-published directory
            raise
        return cls(root)

    @staticmethod
    def part(data, part_id):
        return next(p for p in data['parts'] if p['id'] == part_id)

    @staticmethod
    def current(part):
        return next((v for v in part['versions'] if v['id'] == part['selected'] and v['text'] == part['text']), None)

    def edit(self, part_id, text, expected_revision=None):
        data = self.read()
        if expected_revision is not None and data['revision'] != expected_revision:
            raise ValueError('项目已在别处更新，请重新打开；当前编辑尚未覆盖已有内容。')
        part = self.part(data, part_id)
        text = validate_text(text)
        if text != part['text']:
            part['text'] = text
            part['selected'] = None
            self.save(data, data['revision'])

    def choose(self, part_id, version_id, expected_revision=None):
        data = self.read()
        if expected_revision is not None and data['revision'] != expected_revision:
            raise ValueError('项目已在别处更新，请重新打开后再选择版本。')
        part = self.part(data, part_id)
        version = next(v for v in part['versions'] if v['id'] == version_id)
        self.checked_audio(version)
        part['selected'], part['text'] = version_id, version['text']
        self.save(data, data['revision'])

    def checked_audio(self, version):
        path = self.file(version['path'])
        if not path.is_file() or digest(path) != version['sha256']:
            raise ValueError('这版音频已被移动或改动，请重新选择或生成；其他版本仍保留。')
        return path

    def generate(self, part_id, engine, progress=lambda *_: None, cancelled=lambda: False):
        data = self.read()
        part = self.part(data, part_id)
        for ref in data['references'].values():
            path = self.file(ref['path'])
            if not path.is_file() or digest(path) != ref['sha256']:
                raise ValueError('项目参考音频已被改动，请使用完整项目副本。')
        profile = dict(data['voice'])
        profile['reference_audio_path'] = str(self.file(data['references']['voice']['path']))
        options = deepcopy(data['options'])
        if 'emotion' in data['references']:
            options['emotion_audio_path'] = str(self.file(data['references']['emotion']['path']))
        options.update(model_version='2.5', device='cpu', use_persistent_worker=True, timeout_seconds=1800)
        version_id = uuid.uuid4().hex
        path = self.file('versions/' + version_id + '.wav')
        if cancelled():
            raise ProjectCancelled('已取消，已有版本保留。')
        result = engine.synthesize(SynthesisRequest(part['text'], VoiceProfile(**profile), path,
                                                    engine_options=options), progress, cancelled)
        if cancelled():
            raise ProjectCancelled('已取消，已有版本保留。')
        if Path(result.wav_path).resolve() != path.resolve():
            raise ValueError('生成结果位置与项目不符，未替换已有版本。')
        info = inspect_wav(path)
        version = {'id': version_id, 'path': str(path.relative_to(self.root)), 'text': part['text'],
                   'sha256': digest(path), 'created': datetime.now().isoformat(), **info}
        # Preserve actual model evidence outside the compact UI manifest.
        self._write(path.with_suffix('.json'), {'version': version, 'result_meta': result.meta})
        part['versions'].append(version)
        if part['selected'] is None:
            part['selected'] = version_id
        self.save(data, data['revision'])
        return version

    def export(self, destination, gap_seconds=.2, cancelled=lambda: False):
        gap = float(gap_seconds)
        if not math.isfinite(gap) or not 0 <= gap <= 2:
            raise ValueError('段落间隔需在 0～2 秒之间。')
        data = self.read()
        chosen = [self.current(p) for p in data['parts']]
        if not all(chosen):
            raise ValueError('还有段落未生成，或文案修改后未重新生成。')
        paths = [self.checked_audio(v) for v in chosen]
        destination = Path(destination).expanduser()
        if destination.suffix.lower() != '.wav':
            raise ValueError('请选择 WAV 文件。')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError('这个文件名已存在，请换一个，原文件不会覆盖。')
        tmp = None
        total = 0
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.wav', delete=False) as f:
                tmp = Path(f.name)
            with wave.open(str(tmp), 'wb') as out:
                shape = None
                for i, path in enumerate(paths):
                    with wave.open(str(path), 'rb') as src:
                        current = (src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getcomptype())
                        if current[1] != 2 or current[3] != 'NONE':
                            raise ValueError('音频格式不支持无损合并。')
                        if shape is None:
                            shape = current
                            out.setparams(src.getparams())
                        elif shape != current:
                            raise ValueError('各段音频格式不同，未合并，请检查项目。')
                        if i:
                            frames = round(gap * shape[2])
                            out.writeframesraw(b'\0' * frames * shape[0] * shape[1])
                            total += frames
                        while raw := src.readframes(32768):
                            if cancelled():
                                raise ProjectCancelled('已取消导出，原文件保留。')
                            out.writeframesraw(raw)
                            total += len(raw) // (shape[0] * shape[1])
            # Recheck after copying, before atomically publishing a new file.
            for version in chosen:
                self.checked_audio(version)
            with self.locked():
                if self.read()['revision'] != data['revision']:
                    raise ValueError('导出期间项目已变化，请重试。')
                if cancelled():
                    raise ProjectCancelled('已取消导出，原文件保留。')
                with tmp.open('rb') as f:
                    os.fsync(f.fileno())
                os.link(tmp, destination)  # exclusive creation, never replace an existing export
                receipt = {'path': str(destination.resolve()), 'duration': total / shape[2],
                           'sha256': digest(destination), 'versions': [v['id'] for v in chosen], 'gap_seconds': gap}
                data['last_export'] = receipt
                data['gap_seconds'] = gap
                data['revision'] += 1
                self._write(self.path, data)
            return receipt
        finally:
            if tmp and tmp.exists():
                tmp.unlink()
