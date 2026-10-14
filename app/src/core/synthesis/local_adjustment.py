"""Replace a confirmed sample range; preserve every sample outside that range."""
from copy import deepcopy
from contextlib import contextmanager
import os
import math
import uuid
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import wave

from .segment_project import SegmentProject, ProjectCancelled, digest, inspect_wav, validate_text, reference_info


class LocalAdjustment(SegmentProject):
    @classmethod
    def open_audio(cls, parent, source, voice, options, *, legacy_parent=None):
        source = Path(source).expanduser().resolve()
        inspect_wav(source)
        fingerprint = digest(source)
        # The audio identifies its saved adjustment work, including old candidates.
        folder = Path(parent).expanduser() / fingerprint
        if legacy_parent and not (folder / 'adjustment.json').exists():
            legacy = Path(legacy_parent).expanduser() / fingerprint
            if (legacy / 'adjustment.json').is_file():
                folder = legacy
        pointer = folder / 'adjustment.json'
        if pointer.exists():
            import json
            relative = json.loads(pointer.read_text())['project']
            root = (folder / relative).resolve()
            if root.parent != folder.resolve():
                raise ValueError('局部调整记录位置无效。')
            project = cls(root)
            if project.read()['adjustment']['source']['sha256'] != fingerprint:
                raise ValueError('成品与调整记录不一致。')
            project.source_audio()
            return project
        if voice is None:
            raise ValueError('请先在配音页选择要使用的音色，再打开成品。')
        project = super().create(folder, '请填写选中范围内要说的话。', voice, options)
        snapshot = project.file('original.wav')
        shutil.copyfile(source, snapshot)
        if digest(snapshot) != fingerprint or digest(source) != fingerprint:
            raise ValueError('读取期间成品发生变化，请重新打开。')
        info = inspect_wav(snapshot)
        data = project.read()
        data['adjustment'] = {'source': {'path': 'original.wav', 'sha256': fingerprint,
                                       'name': source.name, **info},
                              'start_frame': 0, 'end_frame': info['frames'], 'draft': ''}
        project.save(data, data['revision'])
        # Do not overwrite another session's pointer if it won a simultaneous open.
        with project.locked():
            if pointer.exists():
                raise ValueError('这份成品已在另一窗口打开，请重新打开。')
            with pointer.open('x', encoding='utf-8') as f:
                import json
                json.dump({'project': project.root.name}, f)
        return project

    def source_audio(self):
        return self.checked_audio(self.read()['adjustment']['source'])

    def set_selection(self, start, end, text, expected_revision=None):
        data = self.read()
        state = data['adjustment']
        frames = state['source']['frames']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= frames:
            raise ValueError('请把起点放在终点前，并保持在音频范围内。')
        text = str(text).strip()
        if text:
            validate_text(text)
        if expected_revision is not None and expected_revision != data['revision']:
            raise ValueError('项目已在别处更新，请重新打开。已有声音已保留。')
        if (start, end, text) == (state['start_frame'], state['end_frame'], state['draft']):
            return
        state.update(start_frame=start, end_frame=end, draft=text)
        data['parts'][0]['text'] = text or '请填写选中范围内要说的话。'
        data['parts'][0]['selected'] = None
        self.save(data, data['revision'] if expected_revision is None else expected_revision)

    def set_emotion(self, emotion, expected_revision=None):
        data = self.read()
        if expected_revision is not None and expected_revision != data['revision']:
            raise ValueError('项目已在别处更新，请重新打开。')
        mode = emotion.get('emotion_mode', 'follow')
        if mode not in ('follow', 'emoaudio', 'vector', 'emotext'):
            raise ValueError('请选择有效的情绪表达。')
        clean = {'emotion_mode': mode, 'random_emotion': bool(emotion.get('random_emotion', False))
                 if mode in ('vector', 'emotext') else False}
        if mode != 'follow':
            weight = float(emotion.get('emotion_weight', .8))
            if not math.isfinite(weight) or not 0 <= weight <= 1.6:
                raise ValueError('情绪强度需在 0～1.6 之间。')
            clean['emotion_weight'] = weight
        if mode == 'vector':
            from .indextts2_presets import normalize_emotion_vector
            values = list(normalize_emotion_vector(emotion.get('emotion_vector')))
            if not all(math.isfinite(v) for v in values):
                raise ValueError('情绪数值无效。')
            clean['emotion_vector'] = values
        if mode == 'emotext':
            clean['emotion_text'] = str(emotion.get('emotion_text', '')).strip()
            if not clean['emotion_text']:
                raise ValueError('请写下这段要表达的情绪。')
        refs = deepcopy(data['references'])
        refs.pop('emotion', None)
        if mode == 'emoaudio':
            source = Path(str(emotion.get('emotion_audio_path', ''))).expanduser()
            if not source.is_file():
                raise ValueError('请选择情绪参考音频。')
            relative = 'references/emotion_' + uuid.uuid4().hex + source.suffix.lower()
            target = self.file(relative)
            try:
                shutil.copyfile(source, target)
                info = reference_info(target)
                refs['emotion'] = {'path': relative, 'sha256': digest(target), **info}
            except Exception:
                target.unlink(missing_ok=True)
                raise
            clean['emotion_audio_path'] = relative
        # Older candidates predate settings snapshots; preserve their original
        # settings before the first edit so they remain restorable.
        for version in data['parts'][0]['versions']:
            version.setdefault('options', deepcopy(data['options']))
            version.setdefault('references', deepcopy(data['references']))
        data['options'] = {k: v for k, v in data['options'].items()
                           if not k.startswith('emotion_') and k not in ('random_emotion', 'style_preset')}
        data['options'].update(clean)
        data['references'] = refs
        data['parts'][0]['selected'] = None
        self.save(data, data['revision'])

    def restore_candidate(self, version_id, expected_revision=None):
        data = self.read()
        version = self.candidate(version_id)
        if expected_revision is not None and expected_revision != data['revision']:
            raise ValueError('项目已在别处更新，请重新打开。')
        data['adjustment'].update(**version['range'], draft=version['text'])
        data['parts'][0]['text'] = version['text']
        data['parts'][0]['selected'] = None
        data['options'] = deepcopy(version.get('options', data['options']))
        data['references'] = deepcopy(version.get('references', data['references']))
        self.save(data, data['revision'])

    def settings_match(self, version):
        return version.get('options', self.read()['options']) == self.read()['options']

    def generate_candidate(self, engine, progress=lambda *_: None, cancelled=lambda: False):
        before = self.read()
        self.source_audio()
        state = deepcopy(before['adjustment'])
        validate_text(state['draft'])
        version = super().generate(before['parts'][0]['id'], engine, progress, cancelled)
        data = self.read()
        if data['revision'] != before['revision'] + 1:
            raise ValueError('生成期间调整范围已变化，新声音已保留，请重新打开。')
        stored = next(v for v in data['parts'][0]['versions'] if v['id'] == version['id'])
        stored['range'] = {k: state[k] for k in ('start_frame', 'end_frame')}
        stored['source_sha256'] = state['source']['sha256']
        stored['options'] = deepcopy(before['options'])
        stored['references'] = deepcopy(before['references'])
        # Generating is never adoption, even for the first candidate.
        data['parts'][0]['selected'] = None
        self.save(data, data['revision'])
        return stored

    def candidate(self, version_id):
        data = self.read()
        version = next((v for v in data['parts'][0]['versions'] if v['id'] == version_id), None)
        if not version or 'range' not in version:
            raise ValueError('请选择已完成的候选声音。')
        state = data['adjustment']
        if version['source_sha256'] != state['source']['sha256']:
            raise ValueError('候选不属于这份成品。')
        start, end = (version['range'][k] for k in ('start_frame', 'end_frame'))
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= state['source']['frames']:
            raise ValueError('候选的声音范围无效。')
        return version

    @staticmethod
    def _copy_frames(source, output, count, cancelled):
        while count:
            if cancelled():
                raise ProjectCancelled('已取消，原版保留。')
            n = min(count, 32768)
            raw = source.readframes(n)
            if len(raw) != n * source.getnchannels() * source.getsampwidth():
                raise ValueError('音频数据不完整，原版保留。')
            output.writeframesraw(raw)
            count -= n

    @contextmanager
    def _replacement_format(self, replacement, source, cancelled):
        if replacement is None:
            yield None
            return
        with wave.open(str(replacement), 'rb') as take:
            if (take.getnchannels(), take.getframerate(), take.getsampwidth()) == (
                    source.getnchannels(), source.getframerate(), source.getsampwidth()):
                yield replacement
                return
        # Adapt only the new speech to the original WAV. Never resample the
        # original prefix/suffix, and never change the candidate's stored file.
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory(prefix='format_', dir=self.root) as temp:
            converted = Path(temp) / 'replacement.wav'
            with (Path(temp) / 'error.log').open('wb') as log:
                process = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-nostdin',
                    '-i', str(replacement), '-vn', '-ac', str(source.getnchannels()),
                    '-ar', str(source.getframerate()), '-c:a', 'pcm_s16le', str(converted)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
                deadline = time.monotonic() + 120
                try:
                    while process.poll() is None:
                        if cancelled():
                            raise ProjectCancelled('已取消，原版保留。')
                        if time.monotonic() > deadline:
                            raise ValueError('音频格式转换超时，原版保留。')
                        try:
                            process.wait(timeout=.1)
                        except subprocess.TimeoutExpired:
                            pass
                    if process.returncode:
                        raise ValueError('新声音暂时无法转换为成品的音频格式，原版保留。')
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            yield converted

    def render(self, destination, *, version_id=None, start=None, end=None,
               context=False, cancelled=lambda: False):
        """Export a full candidate, or audition a bounded original/candidate seam."""
        source = self.source_audio()
        data = self.read()
        state = data['adjustment']
        version = self.candidate(version_id) if version_id else None
        replacement = self.checked_audio(version) if version else None
        if version:
            start, end = (version['range'][k] for k in ('start_frame', 'end_frame'))
        frames = state['source']['frames']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= frames:
            raise ValueError('请选择有效的声音范围。')
        target = Path(destination).expanduser()
        if target.suffix.lower() != '.wav' or target.exists():
            raise ValueError('请使用新的 WAV 文件名，原文件不会覆盖。')
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.wav', delete=False) as f:
                temp = Path(f.name)
            with wave.open(str(source), 'rb') as src, wave.open(str(temp), 'wb') as out:
                out.setparams(src.getparams())
                rate = src.getframerate()
                left = max(0, start - rate) if context else (0 if version else start)
                right = min(frames, end + rate) if context else (frames if version else end)
                src.setpos(left)
                self._copy_frames(src, out, start - left, cancelled)
                if replacement:
                    with self._replacement_format(replacement, src, cancelled) as adapted, wave.open(str(adapted), 'rb') as take:
                        shape = lambda f: (f.getnchannels(), f.getsampwidth(), f.getframerate(), f.getcomptype())
                        if shape(src) != shape(take):
                            raise ValueError('候选与成品的音频格式不同，暂时无法替换；原版保留。')
                        self._copy_frames(take, out, take.getnframes(), cancelled)
                        replacement_frames = take.getnframes()
                    src.setpos(end)
                else:
                    self._copy_frames(src, out, end - start, cancelled)
                    replacement_frames = end - start
                self._copy_frames(src, out, right - end, cancelled)
            self.source_audio()
            if version:
                self.checked_audio(version)
            if cancelled():
                raise ProjectCancelled('已取消，原版保留。')
            with temp.open('rb') as f:
                os.fsync(f.fileno())
            os.link(temp, target)
            return {'path': str(target.resolve()), 'duration': (start - left + replacement_frames + right - end) / rate,
                    'sha256': digest(target), 'version_id': version_id,
                    'start_frame': start, 'end_frame': end, 'sample_rate': rate}
        finally:
            if temp and temp.exists():
                temp.unlink()

    def adopt(self, destination, version_id, cancelled=lambda: False):
        data = self.read()
        version = self.candidate(version_id)
        current = data['adjustment']
        if (not self.settings_match(version) or version['text'] != current['draft'] or
                version['range'] != {k: current[k] for k in ('start_frame', 'end_frame')}):
            raise ValueError('范围或文案、情绪设置已改变，请先选回这版候选再采用。')
        # Exclude concurrent selection changes for the publication and receipt.
        with self.locked():
            if self.read()['revision'] != data['revision']:
                raise ValueError('调整记录已变化，请重新打开。')
            receipt = self.render(destination, version_id=version_id, cancelled=cancelled)
            data['adjustment']['last_export'] = receipt
            data['parts'][0]['selected'] = version_id
            data['revision'] += 1
            try:
                self._write(self.path, data)
            except Exception as exc:
                raise RuntimeError(f'新成品已保存到 {receipt["path"]}，但调整记录保存失败：{exc}') from exc
        return receipt
