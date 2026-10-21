"""The two native ASR adapters. They share process ownership, not parsers."""
import json
import math
from pathlib import Path
import re
import wave

from .base import TranscriptionEngine
from .asr_resource import model_use
from .native_process import run_native, NativeProcessError
from ..models import Segment, Transcript


class IncompleteTranscription(RuntimeError):
    def __init__(self, message, text='', raw_text=''):
        self.partial_text = text
        self.raw_text = raw_text
        super().__init__(message)


def moss_preflight(duration):
    """Pinned Q5 metadata + upstream prompt bound; no memory-size extrapolation."""
    import subprocess
    # 12.5 audio tokens/sec, 15+71 prompt tokens, 256 reserved by this runtime.
    # Round up to a complete 30-second encoder window as a conservative bound.
    tokens = math.ceil(duration / 30) * 375
    if tokens > 131072:
        raise ValueError('录音超过 MOSS 工具的整段处理范围，请选择 Q8 或 Whisper。')
    markers = sum(len(str(sec)) for sec in range(5, int(tokens / 12.5) + 1, 5))
    if tokens + markers + 86 + 256 > 131072:
        raise ValueError('录音超过 MOSS 工具的整段处理范围，请选择 Q8 或 Whisper。')
    try:
        state = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'],
                               capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('无法检查当前内存状态，MOSS 未开始转录；请重试或选择 Q8、Whisper。') from exc
    if state != '1':
        raise RuntimeError('当前内存压力较高，MOSS 未开始转录；请关闭其他占用内存的应用，或选择 Q8、Whisper。')


def parse_moss(stdout, source, duration):
    rows = []
    header_seen = False
    try:
        for line in stdout.splitlines():
            value = json.loads(line)
            if type(value) is not dict: raise ValueError()
            if value.get('type') == 'batch_header':
                if set(value) != {'type', 'load_ms'} or rows or header_seen: raise ValueError()
                header_seen = True
                continue
            rows.append(value)
        if len(rows) != 1 or rows[0].get('file') != str(source): raise ValueError()
    except (ValueError, TypeError) as exc:
        raise IncompleteTranscription('MOSS 返回的结果无法完整核对，原文件保留。') from exc
    row = rows[0]
    text = row.get('text')
    raw = row.get('raw_text')
    recovery_raw = raw if type(raw) is str else ''
    if type(text) is not str: raise IncompleteTranscription('MOSS 正文无效。', raw_text=recovery_raw)
    if row.get('error'):
        raise IncompleteTranscription('MOSS 提前停止，部分文字未作为完整文稿保存。', text, recovery_raw)
    segments = row.get('segments')
    if not text.strip() or not isinstance(segments, list) or not segments:
        raise IncompleteTranscription('MOSS 没有返回有效的分段结果，请换用其他本地模型。', text, recovery_raw)
    result = []
    try:
        for item in segments:
            if (type(item) is not dict or set(item) - {'text', 't0_ms', 't1_ms', 'speaker_id'}
                    or type(item.get('text')) is not str): raise ValueError()
            a, b, speaker = item.get('t0_ms'), item.get('t1_ms'), item.get('speaker_id')
            if (type(a) is not int or type(b) is not int or not 0 <= a <= b
                    or b > math.ceil(duration * 1000) + 100): raise ValueError()
            if speaker is not None and (type(speaker) is not int or not 0 < speaker <= 2147483647): raise ValueError()
            if item['text'].strip(): result.append(Segment(item['text'].strip(), a/1000, b/1000, speaker))
        # Verify that a renderer will not drop part of the clean transcript.
        if re.sub(r'\s+', '', text) != re.sub(r'\s+', '', ''.join(s.text for s in result)): raise ValueError()
        if not result or not all(s.end > s.start for s in result): raise ValueError()
        if re.search(r'\[S\d+\]|<\|[^>]*\|>', text): raise ValueError()
        # The upstream parser fills missing boundaries. Accept timed results only
        # when every turn is present with its own boundaries in the raw output.
        if type(raw) is not str: raise ValueError()
        matches = list(re.finditer(r'\[(\d+(?:\.\d+)?)\]\[S(\d+)\](.*?)\[(\d+(?:\.\d+)?)\]', raw, re.S))
        position = 0
        for match in matches:
            if raw[position:match.start()].strip(): raise ValueError()
            position = match.end()
        if raw[position:].strip(): raise ValueError()
        turns = [match.groups() for match in matches]
        if len(turns) != len(result): raise ValueError()
        for (start, speaker, body, end), segment in zip(turns, result):
            if (abs(float(start) - segment.start) > .001 or abs(float(end) - segment.end) > .001
                    or (segment.speaker_id is not None and int(speaker) != segment.speaker_id)
                    or re.sub(r'\s+', '', body) != re.sub(r'\s+', '', segment.text)):
                raise ValueError()
    except (ValueError, TypeError) as exc:
        raise IncompleteTranscription('MOSS 的时间或发言人结果不完整，部分文字已保留。', text, recovery_raw) from exc
    return Transcript(text.strip(), result, None, 'local_moss_q5')


class NativeASREngine(TranscriptionEngine):
    requires_network = False

    def __init__(self, model_id):
        from .local_models import LOCAL_MODELS
        self.name = model_id
        self.display_name = LOCAL_MODELS[model_id].display_name

    def transcribe(self, wav_path, progress=None, is_cancelled=None):
        try:
            return self._transcribe(wav_path, progress, is_cancelled)
        except IncompleteTranscription as exc:
            if not (exc.partial_text.strip() or exc.raw_text.strip()): raise
            from ..models_manager import app_data_dir
            import os
            import stat
            from uuid import uuid4
            directory = app_data_dir() / 'incomplete-transcriptions'
            try:
                if any(p.is_symlink() for p in (directory, *directory.parents)):
                    raise OSError('unsafe recovery directory')
                directory.mkdir(parents=True, mode=0o700, exist_ok=True)
                info = directory.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise OSError('invalid recovery permissions')
                path = directory / (uuid4().hex + '.txt')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as out:
                    out.write('未完成的转录，仅供恢复参考。\n\n' + exc.partial_text)
                    if exc.raw_text:
                        out.write('\n\n原始返回（可能含未闭合文字和无效时间，仅供核对）：\n' + exc.raw_text)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as storage_error:
                raise IncompleteTranscription('转录未完成，部分文字也未能写入恢复文件；原音频保留。', exc.partial_text, exc.raw_text) from storage_error
            raise IncompleteTranscription(f'转录未完成，部分文字已保存在：{path}', exc.partial_text, exc.raw_text) from exc

    def _transcribe(self, wav_path, progress=None, is_cancelled=None):
        from .. import models_manager as mm
        from ..artifact_trust import model_revision
        from .local_models import LOCAL_MODELS
        progress = progress or (lambda *_: None)
        cancelled = is_cancelled or (lambda: False)
        with model_use(self.name):
            if cancelled(): raise InterruptedError('转录已取消')
            if not mm.is_downloaded(self.name): raise RuntimeError('所选本地模型尚未校验就绪。')
            root = mm.model_path(self.name)
            with wave.open(str(wav_path), 'rb') as wav:
                if wav.getframerate() != 16000 or wav.getnchannels() != 1:
                    raise ValueError('转录输入必须是已准备好的 16k 单声道音频。')
                duration = wav.getnframes()/16000
            progress(f'正在转录（{self.display_name}）…', -1)
            if self.name == 'local_sensevoice_q8':
                args = lambda work: ['-m', str(root/'sensevoice-small-q8.gguf'), '-a', str(wav_path),
                    '--vad', str(root/'fsmn-vad.gguf'), '--vad-maxseg', '30000']
                text, version = run_native('sensevoice-q8', args, is_cancelled=cancelled, progress=progress)
                text = re.sub(r'<\|[^>]*\|>', '', text).strip()
                result = Transcript(text, [Segment(x.strip()) for x in text.splitlines() if x.strip()], None, self.name)
            else:
                moss_preflight(duration)
                def args(work):
                    (work/'input.txt').write_text(str(wav_path)+'\n')
                    return ['-m', str(root/'MOSS-Transcribe-Diarize-Q5_K_M.gguf'), '--backend', 'metal',
                            '--threads', '8', '--timestamps', 'segment', '--diarize', '--batch', str(work/'input.txt'), '--batch-jsonl']
                try:
                    stdout, version = run_native('moss', args, is_cancelled=cancelled, progress=progress)
                except NativeProcessError as exc:
                    raw_partial = ''
                    try:
                        partial = parse_moss(exc.stdout, wav_path, duration).text
                    except IncompleteTranscription as incomplete:
                        partial = incomplete.partial_text
                        raw_partial = incomplete.raw_text
                    raise IncompleteTranscription(str(exc), partial, raw_partial) from exc
                result = parse_moss(stdout, wav_path, duration)
            if cancelled(): raise InterruptedError('转录已取消')
            m = LOCAL_MODELS[self.name]
            result.meta['asr'] = dict(model_id=self.name, model_revision=model_revision(m.repo, m.repo_source),
                runtime_version=version, timestamp_kind='segment' if m.timestamps else 'none',
                diarization=self.name == 'local_moss_q5', timing_estimated=False)
            return result
