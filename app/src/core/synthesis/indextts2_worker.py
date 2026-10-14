"""Standalone IndexTTS2 subprocess worker.

This module intentionally uses only the Python standard library. It must not
import Lulu UI modules or IndexTTS2 packages into the main application process.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import wave
import warnings
from pathlib import Path
from typing import Any

QUALITY_CPU_MESSAGE = '完整参考模式 · CPU 生成较慢；参考按官方最多使用前 15 秒，原录音保留'

# Standard-library only: shared with the UI/adapter, and available when this
# packaged file runs alone under the isolated model Python (-I).
# (default, minimum, maximum); numeric limits follow the official WebUI and
# pinned model configs. interval_silence is an infer option with a Lulu UI cap.
INDEX2_GENERATION_FIELDS = {
    'temperature': (0.8, 0.1, 2.0),
    'top_p': (0.8, 0.0, 1.0),
    'top_k': (30, 0, 100),
    'repetition_penalty': (10.0, 0.1, 20.0),
    'length_penalty': (0.0, -2.0, 2.0),
    'num_beams': (3, 1, 10),
    'max_text_tokens_per_segment': (120, 20, 600),
    'max_mel_tokens': (1500, 50, 1815),
    'interval_silence': (200, 0, 2000),
}


# The pinned 2.5 infer consumes the same public numeric options/defaults.
# Keep a separate table so future version-specific changes cannot alter 2.
INDEX25_GENERATION_FIELDS = dict(INDEX2_GENERATION_FIELDS)


def validate_index_generation(value, model_version='2.0'):
    if model_version not in ('2.0', '2.5'):
        raise ValueError('不支持的 IndexTTS 模型版本。')
    fields = INDEX25_GENERATION_FIELDS if model_version == '2.5' else INDEX2_GENERATION_FIELDS
    return _validate_generation(value, fields)


def validate_index2_generation(value):
    return validate_index_generation(value, '2.0')


def _validate_generation(value, fields):
    """Sparse overrides preserve the approved baseline, including legacy gaps."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('高级设置格式不正确，请恢复默认设置。')
    result = {}
    for key, number in value.items():
        if key not in fields:
            raise ValueError('存在不支持的生成设置。')
        default, minimum, maximum = fields[key]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not minimum <= number <= maximum or not math.isfinite(number)
                or (isinstance(default, int) and number != int(number))):
            raise ValueError(f'{key} 应在 {minimum} 至 {maximum} 之间' + ('，且为整数。' if isinstance(default, int) else '。'))
        result[key] = int(number) if isinstance(default, int) else float(number)
    return result


def _segment_gap(model_version, speech):
    options = validate_index_generation((speech or {}).get('generation'), model_version)
    return options['interval_silence'] / 1000 if 'interval_silence' in options else 0.16

_PATH_PATTERNS = (
    re.compile(r"(/Users/[^\n'\"，。；;]+)"),
    re.compile(r"(/[A-Za-z0-9._~+/%-]+(?:/[A-Za-z0-9._~+/%-]+)+)"),
    re.compile(r"([A-Za-z]:\\[^\s'\"，。；;]+)"),   # Windows 盘符路径
)
_SECRET_PATTERNS = (
    re.compile(r"Authorization\s*[:=]\s*\S+(?:\s+\S+)?", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key|token)\s*[:=]\s*\S+", re.IGNORECASE),
)
_USER_TEXT_PATTERNS = (
    re.compile(r"\b(input\s*text)\s*[:=]\s*.*", re.IGNORECASE),
    re.compile(r"\b(prompt)\s*[:=]\s*.*", re.IGNORECASE),
)
_WARNING_LINE_PATTERNS = (
    re.compile(r"GPT2InferenceModel has generative capabilities", re.IGNORECASE),
    re.compile(r"past_key_values is deprecated", re.IGNORECASE),
    re.compile(r"FutureWarning", re.IGNORECASE),
    re.compile(r"UserWarning", re.IGNORECASE),
)


def _scrub_detail(text: str, limit: int = 500) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) > 1:
        kept = [
            line for line in lines
            if not any(pattern.search(line) for pattern in _WARNING_LINE_PATTERNS)
        ]
        cleaned = "\n".join(kept or lines[-3:])
    else:
        cleaned = str(text or "")
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub("[已隐藏凭据]", cleaned)
    for pat in _USER_TEXT_PATTERNS:
        cleaned = pat.sub(lambda match: f"{match.group(1)}: [已隐藏正文]", cleaned)
    for pat in _PATH_PATTERNS:
        cleaned = pat.sub("[本地路径]", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:180]
    tail = cleaned[-(limit - len(head) - 12):]
    return f"{head} ... {tail}"


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def _progress(job_id: str, stage: str, message: str, fraction: float, started: float) -> None:
    _emit(
        {
            "type": "progress",
            "job_id": job_id,
            "stage": stage,
            "message": message,
            "fraction": fraction,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
    )


def _error(job_id: str, code: str, message: str, detail: str, retryable: bool = False) -> int:
    if 'INCOMPLETE_AUDIO' in detail:
        code = 'INCOMPLETE_AUDIO'
        message = '模型未完整读完，已停止保存；请缩短当前句子后重试。'
    _emit(
        {
            "type": "error",
            "job_id": job_id,
            "code": code,
            "message": message,
            "detail_redacted": _scrub_detail(detail),
            "retryable": retryable,
        }
    )
    return 1


def _duration_and_rate(wav_path: Path) -> tuple[float, int]:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        return (frames / float(rate)) if rate else 0.0, rate
    except Exception:
        return 0.0, 0


def _prepare_cli_home(base_dir: Path) -> dict[str, str]:
    cli_home = (base_dir / "indextts_home").resolve(strict=False)
    cli_home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(cli_home)
    os.environ.setdefault("XDG_CONFIG_HOME", str((cli_home / ".config").resolve(strict=False)))
    os.environ.setdefault("XDG_DATA_HOME", str((cli_home / ".local" / "share").resolve(strict=False)))
    return os.environ.copy()


def _stitch_wavs(parts: list[Path], dst: Path, gap_seconds: float = 0.16) -> tuple[float, int]:
    if not parts:
        raise RuntimeError("没有可拼接的音频片段。")
    params = None
    frames: list[bytes] = []
    sample_rate = 0
    for i, part in enumerate(parts):
        with wave.open(str(part), "rb") as wf:
            current = wf.getparams()
            if params is None:
                params = current
                sample_rate = wf.getframerate()
            elif (
                wf.getnchannels() != params.nchannels
                or wf.getsampwidth() != params.sampwidth
                or wf.getframerate() != params.framerate
            ):
                raise RuntimeError(f"分段音频格式不一致:{part}")
            if i > 0 and gap_seconds > 0:
                frames.append(b"\x00" * int(sample_rate * gap_seconds) * params.nchannels * params.sampwidth)
            frames.append(wf.readframes(wf.getnframes()))
    assert params is not None
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as out:
        out.setparams(params)
        out.writeframes(b"".join(frames))
    duration, rate = _duration_and_rate(dst)
    return duration, rate


def _runtime_python(runtime_dir: Path) -> Path:
    if os.name == "nt":
        return runtime_dir / "python" / "python.exe"
    binp = runtime_dir / "python" / "bin"
    cand = sorted(binp.glob("python3.*")) if binp.is_dir() else []
    return cand[0] if cand else binp / "python3"


def _vendor_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "vendor" / "index-tts"


def _nltk_data_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "python" / "nltk_data"


def _ensure_runtime_paths(payload: dict[str, Any], temp_parent: Path) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    runtime_dir = Path(str(payload.get("runtime_dir") or "")).expanduser()
    runtime_python = Path(str(payload.get("runtime_python") or _runtime_python(runtime_dir))).expanduser()
    vendor_dir = Path(str(payload.get("vendor_dir") or _vendor_dir(runtime_dir))).expanduser()
    nltk_data_dir = Path(str(payload.get("nltk_data_dir") or _nltk_data_dir(runtime_dir))).expanduser()
    model_dir = Path(str(payload.get("model_dir") or "")).expanduser()
    env = _prepare_cli_home(temp_parent)
    # Numba otherwise rewrites shipped .nbc/.nbi files under librosa, invalidating
    # the runtime receipt after a successful generation. Keep all generated
    # code outside that sealed tree, including Python bytecode under -I.
    env['NUMBA_CACHE_DIR'] = str((temp_parent / 'numba-cache').resolve())
    env['MPLCONFIGDIR'] = str((temp_parent / 'matplotlib').resolve())
    sys.dont_write_bytecode = True
    env["PYTHONPATH"] = str(vendor_dir) + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    env["NLTK_DATA"] = str(nltk_data_dir)
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1")
    # This worker loads the model in-process; returning an unused environment
    # does not make from_pretrained offline.
    os.environ.update(env)
    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
    return runtime_python, vendor_dir, nltk_data_dir, model_dir, env


def _emotion_args(payload: dict[str, Any]) -> tuple[Path | None, str | None, list[float] | None, float, dict[str, Any], str | None]:
    emotion = payload.get("emotion") or {}
    vector = emotion.get("vector") or [0.0] * 8
    weight = float(emotion.get("weight", 0.8))
    control_mode = str(emotion.get("control_mode") or "").strip().lower()
    emotion_audio = str(emotion.get("audio_path") or "")
    emotion_text = str(emotion.get("text") or "")
    emotion_vector: list[float] | None = None
    emotion_path: Path | None = None
    emotion_text_arg: str | None = None
    validation_error: str | None = None
    if control_mode == "follow":
        pass
    elif control_mode == "emoaudio":
        emo_audio_path = Path(emotion_audio).expanduser()
        if not emotion_audio or not emo_audio_path.exists():
            validation_error = "情感参考音频不可用,请重新选择。"
        else:
            emotion_path = emo_audio_path
    elif control_mode == "emotext":
        if not emotion_text.strip():
            validation_error = "请输入情感描述文本。"
        else:
            emotion_text_arg = emotion_text
    else:
        emotion_vector = [float(value) for value in vector]
    return emotion_path, emotion_text_arg, emotion_vector, weight, emotion, validation_error


def _cpu_emotion(factory, model_dir):
    """Keep the small emotion classifier off the already occupied MPS pool."""
    from types import SimpleNamespace
    module = sys.modules[factory.__module__]
    loader = module.AutoModelForCausalLM
    module.AutoModelForCausalLM = SimpleNamespace(from_pretrained=lambda *args, **kwargs:
        loader.from_pretrained(*args, **{**kwargs, 'device_map': 'cpu', 'local_files_only': True}))
    try:
        backend = factory(model_dir)
    finally:
        module.AutoModelForCausalLM = loader
    generate = backend.model.generate
    def bounded_generate(*args, **kwargs):
        return generate(*args, **{**kwargs, 'max_new_tokens': 256})
    backend.model.generate = bounded_generate
    return backend


class _LazyEmotion:
    """Compatibility for the installed runtime which eagerly builds QwenEmotion."""
    def __init__(self, factory, model_dir):
        self.factory, self.model_dir = factory, model_dir
        self.backend = None
        self.cached_text = None
        self.cached_result = None

    def inference(self, text):
        if text != self.cached_text:
            if self.backend is None:
                self.backend = self.factory(self.model_dir)
            try:
                self.cached_result = self.backend.inference(text)
            finally:
                self.release()
                import gc
                gc.collect()
            self.cached_text = text
        return dict(self.cached_result)

    def release(self):
        self.backend = None


def _sentence_token_segments(tokens: list[str], limit: int = 120) -> list[list[str]]:
    """Pack complete normalized sentences without re-tokenizing or changing text.

    The pinned 2.0 splitter eagerly splits at commas before packing, which can
    leave a sentence's short ending alone. Only an over-budget sentence needs
    clause/length splitting. Native inference still owns synthesis and silence.
    """
    if limit < 1:
        raise ValueError('分段长度必须大于零。')
    endings = {'.', '!', '?', '...', '。', '！', '？', '…'}
    closers = endings | {'”', '’', '"', "'", '」', '』', ')', '）', '】', ']'}
    clauses = {',', ';', ':', '，', '；', '：', '\n'}
    units: list[list[str]] = []
    start = index = 0
    while index < len(tokens):
        token = tokens[index].lstrip('▁')
        boundary = token in endings
        if tokens[index] == '.' and index + 1 < len(tokens):
            following = tokens[index + 1]
            # A bare period inside a version/domain is not a sentence ending.
            previous = tokens[index - 1].lstrip('▁') if index else ''
            if (previous and previous[-1].isdigit()) or (
                following and not following.startswith('▁') and following[0].isalnum()
            ):
                boundary = False
        if boundary:
            end = index + 1
            while end < len(tokens) and tokens[end].lstrip('▁') in closers:
                end += 1
            units.append(tokens[start:end])
            start = end
            index = end
        else:
            index += 1
    if start < len(tokens):
        units.append(tokens[start:])

    result: list[list[str]] = []
    for sentence in units:
        offset = 0
        while offset < len(sentence):
            end = min(offset + limit, len(sentence))
            if end < len(sentence):
                cuts = [i + 1 for i in range(offset, end)
                        if sentence[i].lstrip('▁') in clauses]
                if cuts:
                    end = cuts[-1]
            part = sentence[offset:end]
            if result and len(result[-1]) + len(part) <= limit:
                result[-1].extend(part)
            else:
                result.append(list(part))
            offset = end
    return result


def _complete_infer(tts, **kwargs):
    # Upstream normally only warns before writing truncated speech. Never count
    # a generation-limit stop as a successful, complete narration.
    # Pinned 2.5 consumes do_sample but hard-codes True at the GPT boundary
    # (upstream PR #768). Honor explicit choices only; retain official defaults.
    # A worker handles one request at a time. Restore even on cancellation/error.
    original_sampling = None
    original_split = None
    tokenizer = getattr(tts, 'tokenizer', None)
    if (str(getattr(tts, 'model_version', '2.0')) == '2.0'
            and getattr(tts, 'device', '') != 'mps'
            and callable(getattr(tokenizer, 'split_segments', None))):
        original_split = tokenizer.split_segments
        def split_segments(tokenized, max_text_tokens_per_segment=120, quick_streaming_tokens=0):
            # Streaming has a distinct latency contract; Lulu uses full output.
            if quick_streaming_tokens:
                return original_split(tokenized, max_text_tokens_per_segment,
                                      quick_streaming_tokens=quick_streaming_tokens)
            return _sentence_token_segments(tokenized, max_text_tokens_per_segment)
        tokenizer.split_segments = split_segments
    if str(getattr(tts, 'model_version', '2.0')) == '2.5' and 'do_sample' in kwargs:
        sample = kwargs['do_sample']
        if not isinstance(sample, bool):
            raise ValueError('生成采样开关必须为开启或关闭。')
        original_sampling = tts.gpt.inference_speech
        def inference_speech(*args, **options):
            return original_sampling(*args, **{**options, 'do_sample': sample})
        tts.gpt.inference_speech = inference_speech
    try:
        return _checked_infer(tts, **kwargs)
    finally:
        if original_split is not None:
            tokenizer.split_segments = original_split
        if original_sampling is not None:
            tts.gpt.inference_speech = original_sampling


def _checked_infer(tts, **kwargs):
    with warnings.catch_warnings():
        warnings.filterwarnings('error', message=r'WARN: generation stopped due to exceeding `max_mel_tokens`.*', category=RuntimeWarning)
        try:
            return tts.infer(**kwargs)
        except RuntimeWarning as exc:
            raise RuntimeError('INCOMPLETE_AUDIO：模型未完整读完，已停止保存；请缩短当前句子后重试。') from exc


def _safe_infer(tts, *, speech=None, **kwargs):
    if str(getattr(tts, 'model_version', '2.0')) in ('2.0', '2.5'):
        generation = validate_index_generation((speech or {}).get('generation'), str(getattr(tts, 'model_version', '2.0')))
        kwargs.update(generation)
    if (speech or {}).get('random_emotion'):
        kwargs['use_random'] = True
    if str(getattr(tts, 'model_version', '2.0')) == '2.5':
        speech = speech or {}
        language = str(speech.get('language', 'ZH')).upper()
        speed = float(speech.get('speed', 1.0))
        if language not in {'ZH', 'EN', 'JA', 'ES', 'AR'}:
            raise ValueError('请选择 IndexTTS 2.5 支持的配音语言。')
        if not 0.5 <= speed <= 2.0:
            raise ValueError('配音速度应在 0.5 至 2.0 倍之间。')
        kwargs.update(lang=language, duration_factor=1.0 / speed)
        if 'do_sample' in speech:
            kwargs['do_sample'] = speech['do_sample']
    # Never trade sentence context or search quality for a memory target.
    return _complete_infer(tts, **kwargs)


def _cache_emotion_conditioning(get_emovec):
    # This encoder depends only on the reference, never on narration text.
    # Recomputing it for every short segment causes large transient MPS peaks.
    # Keep only the two current conditions (voice + emotion), not an unbounded
    # voice history. Retain identity and version to avoid stale/reused tensors.
    entries = []
    def cached(tensor, lengths):
        version = tensor._version
        shape = tuple(lengths.tolist())
        for index, (source, revision, size, result) in enumerate(entries):
            if source is tensor and revision == version and size == shape:
                entries.append(entries.pop(index))
                return result
        result = get_emovec(tensor, lengths)
        entries.append((tensor, version, shape, result))
        del entries[:-2]
        return result
    return cached


def _inference_only(infer):
    import torch
    # Official infer only guards the generation loop. Reference conditioning
    # otherwise retains training graphs inside its persistent voice cache.
    return torch.no_grad()(infer)


def _create_tts(*, model_dir: Path, device: str, model_version: str = '2.0') -> Any:
    import inspect
    if device == 'mps':
        raise ValueError('IndexTTS 完整配音请使用 CPU；当前 MPS 路径尚未通过完整参数验证。')
    if model_version == '2.5':
        from indextts import infer_v2_5 as infer_v2  # type: ignore
    elif model_version == '2.0':
        from indextts import infer_v2  # type: ignore
    else:
        raise ValueError('不支持的 IndexTTS 模型版本。')

    constructor = infer_v2.IndexTTS2
    options = dict(cfg_path=str(model_dir / 'config.yaml'), model_dir=str(model_dir),
        device=device, use_cuda_kernel=False, use_deepspeed=False)
    options['use_bf16' if model_version == '2.5' else 'use_fp16'] = False
    factory = getattr(infer_v2, 'QwenEmotion', None)
    modern = 'use_qwen_emo' in inspect.signature(constructor).parameters
    if modern:
        options['use_qwen_emo'] = False
    elif factory is not None:
        # No installed runtime files change. This worker is single-threaded;
        # restore the upstream factory immediately after construction.
        infer_v2.QwenEmotion = lambda path: _LazyEmotion(factory, path)
    try:
        tts = constructor(**options)
    finally:
        if factory is not None:
            infer_v2.QwenEmotion = factory
    if modern and factory is not None:
        emotion_factory = (lambda path: _cpu_emotion(factory, path)) if device == 'mps' else factory
        tts.qwen_emo = _LazyEmotion(emotion_factory, str(model_dir / tts.cfg.qwen_emo_path))
    tts.infer = _inference_only(tts.infer)
    return tts


def _infer_kwargs(
    *,
    voice_path: Path,
    text: str,
    output_path: Path,
    emotion_path: Path | None,
    emotion_text: str | None,
    emotion_vector: list[float] | None,
    emotion_weight: float,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "spk_audio_prompt": str(voice_path),
        "text": text,
        "output_path": str(output_path),
        "verbose": False,
    }
    if emotion_path is not None:
        kwargs["emo_audio_prompt"] = str(emotion_path)
        kwargs["emo_alpha"] = emotion_weight
    if emotion_text is not None:
        kwargs["use_emo_text"] = True
        kwargs["emo_text"] = emotion_text
        kwargs["emo_alpha"] = emotion_weight
    if emotion_vector is not None:
        kwargs["emo_vector"] = emotion_vector
        kwargs["emo_alpha"] = emotion_weight
    return kwargs


def _attach_gr_progress(
    tts: Any,
    *,
    job_id: str,
    started: float,
    base: float,
    span: float,
    prefix: str = "",
) -> None:
    last_fraction = -1.0

    def _callback(value: float, desc: str | None = None) -> None:
        nonlocal last_fraction
        try:
            inner = float(value)
        except (TypeError, ValueError):
            inner = -1.0
        if inner < 0:
            mapped = -1.0
        else:
            mapped = max(base, min(base + span, base + inner * span))
            if mapped + 0.0001 < last_fraction:
                mapped = last_fraction
            last_fraction = mapped
        message = str(desc or "生成中").strip() or "生成中"
        if prefix:
            message = f"{prefix}{message}"
        _progress(job_id, "generating", message, mapped, started)

    tts.gr_progress = _callback


def _emit_result(
    *,
    job_id: str,
    started: float,
    temp_wav: Path,
    device: str,
    emotion: dict[str, Any],
    emotion_weight: float,
    segment_generation: bool = False,
    segment_count: int = 0,
    segment_lengths: list[int] | None = None,
    warning_recovered: bool = False,
) -> None:
    duration, sample_rate = _duration_and_rate(temp_wav)
    elapsed = time.perf_counter() - started
    meta: dict[str, Any] = {
        "engine": "local_indextts2",
        "device": device,
        "emotion_preset": str(emotion.get("preset") or "natural"),
        "emotion_intensity": str(emotion.get("intensity") or "standard"),
        "emotion_weight": emotion_weight,
        "emotion_vector": list(emotion.get("vector") or []),
        "emotion_mode": str(emotion.get("mode") or "preset_vector"),
        "emotion_control_mode": str(emotion.get("control_mode") or ""),
        "emotion_text": str(emotion.get("text") or ""),
        "emotion_summary": str(emotion.get("summary") or ""),
        "emotion_source_preset": str(emotion.get("source_preset") or emotion.get("preset") or "natural"),
        "emotion_source_intensity": str(emotion.get("source_intensity") or emotion.get("intensity") or "standard"),
    }
    if segment_generation:
        meta.update(
            {
                "segment_generation": True,
                "segment_count": segment_count,
                "segment_lengths": list(segment_lengths or []),
            }
        )
    if warning_recovered:
        meta["warning_recovered"] = True
    _emit(
        {
            "type": "result",
            "job_id": job_id,
            "wav_path": str(temp_wav),
            "duration_seconds": round(duration, 3),
            "sample_rate": sample_rate,
            "rtf": round(elapsed / duration, 3) if duration else 0.0,
            "meta": meta,
        }
    )


def _run_single_in_process(
    *,
    job_id: str,
    started: float,
    vendor_dir: Path,
    model_dir: Path,
    reference_audio: Path,
    temp_wav: Path,
    text: str,
    device: str,
    emotion_path: Path | None,
    emotion_text: str | None,
    emotion_vector: list[float] | None,
    emotion_weight: float,
    emotion: dict[str, Any],
    model_version: str = '2.0',
    speech: dict[str, Any] | None = None,
) -> int:
    try:
        if str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
    except Exception as exc:  # noqa: BLE001
        return _error(job_id, "RUNTIME_UNAVAILABLE", "IndexTTS2 运行环境不可用。", str(exc), True)

    _progress(job_id, "loading_model", "加载 IndexTTS2", 0.08, started)
    try:
        tts = _create_tts(model_dir=model_dir, device=device, model_version=model_version)
    except Exception as exc:  # noqa: BLE001
        return _error(job_id, "RUNTIME_UNAVAILABLE", "IndexTTS2 模型加载失败。", str(exc), True)

    _attach_gr_progress(tts, job_id=job_id, started=started, base=0.10, span=0.84)
    try:
        _safe_infer(tts, speech=speech,
            **_infer_kwargs(
                voice_path=reference_audio,
                text=text,
                output_path=temp_wav,
                emotion_path=emotion_path,
                emotion_text=emotion_text,
                emotion_vector=emotion_vector,
                emotion_weight=emotion_weight,
            )
        )
    except Exception as exc:  # noqa: BLE001
        if model_version != '2.5' and temp_wav.exists() and _duration_and_rate(temp_wav)[0] > 0 and 'INCOMPLETE_AUDIO' not in str(exc):
            _progress(job_id, "warning_recovered", "生成完成但收到运行时警告，已保留音频", 0.95, started)
            _emit_result(
                job_id=job_id,
                started=started,
                temp_wav=temp_wav,
                device=device,
                emotion=emotion,
                emotion_weight=emotion_weight,
                warning_recovered=True,
            )
            _progress(job_id, "done", "合成完成", 1.0, started)
            return 0
        return _error(job_id, "GENERATION_FAILED", "IndexTTS2 生成失败。", str(exc), True)
    if not temp_wav.exists() or temp_wav.stat().st_size == 0:
        return _error(job_id, "GENERATION_FAILED", "IndexTTS2 未生成有效音频。", str(temp_wav), True)

    _progress(job_id, "saving", "保存音频", 0.95, started)
    _emit_result(
        job_id=job_id,
        started=started,
        temp_wav=temp_wav,
        device=device,
        emotion=emotion,
        emotion_weight=emotion_weight,
    )
    _progress(job_id, "done", "合成完成", 1.0, started)
    return 0


def _run_segmented_in_process(
    *,
    job_id: str,
    started: float,
    vendor_dir: Path,
    model_dir: Path,
    reference_audio: Path,
    temp_wav: Path,
    text_segments: list[str],
    device: str,
    emotion_path: Path | None,
    emotion_text: str | None,
    emotion_vector: list[float] | None,
    emotion_weight: float,
    emotion: dict[str, Any],
    model_version: str = '2.0',
    speech: dict[str, Any] | None = None,
) -> int:
    try:
        if str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
    except Exception as exc:  # noqa: BLE001
        return _error(job_id, "RUNTIME_UNAVAILABLE", "IndexTTS2 运行环境不可用。", str(exc), True)

    _progress(job_id, "loading_model", "加载 IndexTTS2", 0.08, started)
    try:
        tts = _create_tts(model_dir=model_dir, device=device, model_version=model_version)
    except Exception as exc:  # noqa: BLE001
        return _error(job_id, "RUNTIME_UNAVAILABLE", "IndexTTS2 模型加载失败。", str(exc), True)

    parts: list[Path] = []
    count = len(text_segments)
    generation_base = 0.10
    generation_span = 0.84
    for index, segment in enumerate(text_segments, start=1):
        part = temp_wav.with_name(f"{temp_wav.stem}_segment_{index:03d}.wav")
        base = generation_base + ((index - 1) * generation_span / count)
        span = generation_span / count
        _progress(job_id, "generating", f"生成第 {index}/{count} 段", base + 0.05 * span, started)
        _attach_gr_progress(
            tts,
            job_id=job_id,
            started=started,
            base=base,
            span=span,
            prefix=f"第 {index}/{count} 段:",
        )
        try:
            _safe_infer(tts, speech=speech,
                **_infer_kwargs(
                    voice_path=reference_audio,
                    text=segment,
                    output_path=part,
                    emotion_path=emotion_path,
                    emotion_text=emotion_text,
                    emotion_vector=emotion_vector,
                    emotion_weight=emotion_weight,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _error(job_id, "GENERATION_FAILED", f"第 {index}/{count} 段生成失败。", str(exc), True)
        if not part.exists() or part.stat().st_size == 0:
            return _error(job_id, "GENERATION_FAILED", f"第 {index}/{count} 段未生成有效音频。", str(part), True)
        parts.append(part)
        _progress(job_id, "generating", f"第 {index}/{count} 段合成完成", base + span, started)

    _progress(job_id, "stitching", "拼接分段音频", 0.94, started)
    try:
        duration, sample_rate = _stitch_wavs(parts, temp_wav, _segment_gap(model_version, speech))
    except Exception as exc:  # noqa: BLE001
        return _error(job_id, "STITCH_FAILED", "分段音频拼接失败。", str(exc), True)
    _emit_result(
        job_id=job_id,
        started=started,
        temp_wav=temp_wav,
        device=device,
        emotion=emotion,
        emotion_weight=emotion_weight,
        segment_generation=True,
        segment_count=count,
        segment_lengths=[len(seg) for seg in text_segments],
    )
    _progress(job_id, "done", "合成完成", 1.0, started)
    return 0


class _PersistentServer:
    def __init__(self) -> None:
        self.tts: Any | None = None
        self.model_dir: Path | None = None
        self.device = ""
        self.load_count = 0
        self.last_voice = ""
        self.last_emotion_key = ""
        self.model_version = ""

    def _ensure_tts(self, *, job_id: str, started: float, vendor_dir: Path, model_dir: Path, device: str, model_version: str = '2.0') -> bool:
        if self.tts is not None and self.model_dir == model_dir and self.device == device and self.model_version == model_version:
            _progress(job_id, "worker_ready", "复用 IndexTTS2 后台", 0.08, started)
            return True
        if str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
        _progress(job_id, "loading_model", "加载 IndexTTS2 后台", 0.08, started)
        try:
            self.tts = _create_tts(model_dir=model_dir, device=device, model_version=model_version)
            self.model_dir = model_dir
            self.device = device
            self.model_version = model_version
            self.load_count += 1
        except Exception as exc:  # noqa: BLE001
            _error(job_id, "RUNTIME_UNAVAILABLE", "IndexTTS2 模型加载失败。", str(exc), True)
            self.tts = None
            return False
        _emit(
            {
                "type": "lifecycle",
                "job_id": job_id,
                "state": "ready",
                "load_count": self.load_count,
                "message": "IndexTTS2 后台已常驻",
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
        )
        return True

    def handle_generate(self, payload: dict[str, Any]) -> int:
        started = time.perf_counter()
        job_id = str(payload.get("job_id") or "unknown")
        if str(payload.get('model_version', '2.0')) in ('2.0', '2.5'):
            try:
                validate_index_generation((payload.get('speech') or {}).get('generation'), str(payload.get('model_version', '2.0')))
            except ValueError as exc:
                return _error(job_id, 'INVALID_REQUEST', '高级设置无效，请恢复默认后重试。', str(exc))
        output = payload.get("output") or {}
        temp_wav = Path(str(output.get("temp_wav_path") or "")).expanduser()
        temp_wav.parent.mkdir(parents=True, exist_ok=True)
        try:
            _, vendor_dir, _, model_dir, _ = _ensure_runtime_paths(payload, temp_wav.parent)
        except Exception as exc:  # noqa: BLE001
            return _error(job_id, "RUNTIME_NOT_READY", "IndexTTS2 运行环境尚未安装，请到 AI 大模型配置安装。", str(exc), True)

        voice = payload.get("voice") or {}
        reference_audio = Path(str(voice.get("reference_audio_path") or "")).expanduser()
        text = str(payload.get("text") or "")
        text_segments = [str(seg).strip() for seg in payload.get("text_segments") or [] if str(seg).strip()]
        emotion_path, emotion_text_arg, emotion_vector, weight, emotion, validation_error = _emotion_args(payload)
        device = str(payload.get("device") or "cpu")
        if device == 'cpu':
            _progress(job_id, 'quality_mode', QUALITY_CPU_MESSAGE, 0.06, started)
        if not vendor_dir.is_dir():
            return _error(job_id, "RUNTIME_NOT_READY", "IndexTTS2 运行环境尚未安装，请到 AI 大模型配置安装。", str(vendor_dir), True)
        if not model_dir.is_dir():
            return _error(job_id, "MODEL_NOT_READY", "IndexTTS2 模型尚未安装，请到 AI 大模型配置安装。", str(model_dir), True)
        if not reference_audio.exists():
            return _error(job_id, "REFERENCE_AUDIO_INVALID", "参考音频不可用,请重新选择。", str(reference_audio), True)
        if not text.strip() and not text_segments:
            return _error(job_id, "TEXT_EMPTY", "请输入要生成的文本。", "empty text", True)
        if validation_error is not None:
            return _error(job_id, "EMOTION_INVALID", validation_error, validation_error, True)
        if not self._ensure_tts(job_id=job_id, started=started, vendor_dir=vendor_dir, model_dir=model_dir, device=device,
                                model_version=str(payload.get('model_version', '2.0'))):
            return 1

        voice_key = str(reference_audio)
        emotion_key = json.dumps(
            {
                "mode": emotion.get("control_mode"),
                "audio": emotion.get("audio_path"),
                "text": emotion.get("text"),
                "vector": emotion.get("vector"),
                "weight": weight,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        cache_msg = "复用音色/情感条件" if voice_key == self.last_voice and emotion_key == self.last_emotion_key else "准备音色/情感条件"
        self.last_voice = voice_key
        self.last_emotion_key = emotion_key
        _progress(job_id, "preparing_prompt", cache_msg, 0.10, started)
        assert self.tts is not None
        if len(text_segments) > 1:
            parts: list[Path] = []
            count = len(text_segments)
            generation_base = 0.12
            generation_span = 0.82
            for index, segment in enumerate(text_segments, start=1):
                part = temp_wav.with_name(f"{temp_wav.stem}_segment_{index:03d}.wav")
                base = generation_base + ((index - 1) * generation_span / count)
                span = generation_span / count
                _attach_gr_progress(
                    self.tts,
                    job_id=job_id,
                    started=started,
                    base=base,
                    span=span,
                    prefix=f"第 {index}/{count} 段:",
                )
                try:
                    _safe_infer(self.tts, speech=payload.get('speech'),
                        **_infer_kwargs(
                            voice_path=reference_audio,
                            text=segment,
                            output_path=part,
                            emotion_path=emotion_path,
                            emotion_text=emotion_text_arg,
                            emotion_vector=emotion_vector,
                            emotion_weight=weight,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    return _error(job_id, "GENERATION_FAILED", f"第 {index}/{count} 段生成失败。", str(exc), True)
                if not part.exists() or part.stat().st_size == 0:
                    return _error(job_id, "GENERATION_FAILED", f"第 {index}/{count} 段未生成有效音频。", str(part), True)
                parts.append(part)
                _progress(job_id, "generating", f"第 {index}/{count} 段合成完成", base + span, started)
            _progress(job_id, "stitching", "拼接分段音频", 0.94, started)
            try:
                _stitch_wavs(parts, temp_wav, _segment_gap(str(payload.get('model_version', '2.0')), payload.get('speech')))
            except Exception as exc:  # noqa: BLE001
                return _error(job_id, "STITCH_FAILED", "分段音频拼接失败。", str(exc), True)
            _emit_result(
                job_id=job_id,
                started=started,
                temp_wav=temp_wav,
                device=device,
                emotion=emotion,
                emotion_weight=weight,
                segment_generation=True,
                segment_count=count,
                segment_lengths=[len(seg) for seg in text_segments],
            )
            _progress(job_id, "done", "合成完成", 1.0, started)
            return 0

        _attach_gr_progress(self.tts, job_id=job_id, started=started, base=0.12, span=0.82)
        try:
            _safe_infer(self.tts, speech=payload.get('speech'),
                **_infer_kwargs(
                    voice_path=reference_audio,
                    text=text,
                    output_path=temp_wav,
                    emotion_path=emotion_path,
                    emotion_text=emotion_text_arg,
                    emotion_vector=emotion_vector,
                    emotion_weight=weight,
                )
            )
        except Exception as exc:  # noqa: BLE001
            if str(payload.get('model_version', '2.0')) != '2.5' and temp_wav.exists() and _duration_and_rate(temp_wav)[0] > 0 and 'INCOMPLETE_AUDIO' not in str(exc):
                _progress(job_id, "warning_recovered", "生成完成但收到运行时警告，已保留音频", 0.95, started)
                _emit_result(
                    job_id=job_id,
                    started=started,
                    temp_wav=temp_wav,
                    device=device,
                    emotion=emotion,
                    emotion_weight=weight,
                    warning_recovered=True,
                )
                _progress(job_id, "done", "合成完成", 1.0, started)
                return 0
            return _error(job_id, "GENERATION_FAILED", "IndexTTS2 生成失败。", str(exc), True)
        if not temp_wav.exists() or temp_wav.stat().st_size == 0:
            return _error(job_id, "GENERATION_FAILED", "IndexTTS2 未生成有效音频。", str(temp_wav), True)
        _progress(job_id, "saving", "保存音频", 0.95, started)
        _emit_result(
            job_id=job_id,
            started=started,
            temp_wav=temp_wav,
            device=device,
            emotion=emotion,
            emotion_weight=weight,
        )
        _progress(job_id, "done", "合成完成", 1.0, started)
        return 0


def run_server() -> int:
    server = _PersistentServer()
    _emit({"type": "lifecycle", "state": "started", "message": "IndexTTS2 后台进程已启动"})
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            _emit({"type": "error", "job_id": "unknown", "code": "BAD_COMMAND", "message": "后台命令不可读。"})
            continue
        cmd = str(command.get("command") or "generate")
        job_id = str(command.get("job_id") or "unknown")
        if cmd == "shutdown":
            _emit({"type": "lifecycle", "job_id": job_id, "state": "shutdown", "message": "IndexTTS2 后台已关闭"})
            return 0
        if cmd == "generate":
            server.handle_generate(command)
            continue
        _emit({"type": "error", "job_id": job_id, "code": "BAD_COMMAND", "message": "不支持的后台命令。"})
    return 0


def run_request(request_path: Path) -> int:
    started = time.perf_counter()
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _error("unknown", "BAD_REQUEST", "IndexTTS2 请求文件不可读。", str(exc), False)

    job_id = str(payload.get("job_id") or "unknown")
    if str(payload.get('model_version', '2.0')) in ('2.0', '2.5'):
        try:
            validate_index_generation((payload.get('speech') or {}).get('generation'), str(payload.get('model_version', '2.0')))
        except ValueError as exc:
            return _error(job_id, 'INVALID_REQUEST', '高级设置无效，请恢复默认后重试。', str(exc))
    runtime_dir = Path(str(payload.get("runtime_dir") or "")).expanduser()
    runtime_python = Path(str(payload.get("runtime_python") or _runtime_python(runtime_dir))).expanduser()
    vendor_dir = Path(str(payload.get("vendor_dir") or _vendor_dir(runtime_dir))).expanduser()
    nltk_data_dir = Path(str(payload.get("nltk_data_dir") or _nltk_data_dir(runtime_dir))).expanduser()
    model_dir = Path(str(payload.get("model_dir") or "")).expanduser()
    voice = payload.get("voice") or {}
    reference_audio = Path(str(voice.get("reference_audio_path") or "")).expanduser()
    output = payload.get("output") or {}
    temp_wav = Path(str(output.get("temp_wav_path") or "")).expanduser()
    text = str(payload.get("text") or "")
    text_segments = [str(seg).strip() for seg in payload.get("text_segments") or [] if str(seg).strip()]
    emotion = payload.get("emotion") or {}
    vector = emotion.get("vector") or [0.0] * 8
    weight = float(emotion.get("weight", 0.8))
    control_mode = str(emotion.get("control_mode") or "").strip().lower()
    emotion_audio = str(emotion.get("audio_path") or "")
    emotion_text = str(emotion.get("text") or "")
    device = str(payload.get("device") or "cpu")
    options = payload.get("options") or {}

    _progress(job_id, "checking_runtime", "检查 IndexTTS2 运行环境", 0.05, started)
    if device == 'cpu':
        _progress(job_id, 'quality_mode', QUALITY_CPU_MESSAGE, 0.06, started)
    if not runtime_dir.is_dir() or not runtime_python.exists() or not vendor_dir.is_dir():
        return _error(job_id, "RUNTIME_NOT_READY", "IndexTTS2 运行环境尚未安装，请到 AI 大模型配置安装。", str(runtime_dir), True)
    if not model_dir.is_dir():
        return _error(job_id, "MODEL_NOT_READY", "IndexTTS2 模型尚未安装，请到 AI 大模型配置安装。", str(model_dir), True)
    if not reference_audio.exists():
        return _error(job_id, "REFERENCE_AUDIO_INVALID", "参考音频不可用,请重新选择。", str(reference_audio), True)
    if not text.strip():
        return _error(job_id, "TEXT_EMPTY", "请输入要生成的文本。", "empty text", True)

    temp_wav.parent.mkdir(parents=True, exist_ok=True)
    _ensure_runtime_paths(payload, temp_wav.parent)
    emotion_vector: list[float] | None = None
    emotion_path: Path | None = None
    emotion_text_arg: str | None = None
    if control_mode == "follow":
        pass
    elif control_mode == "emoaudio":
        emo_audio_path = Path(emotion_audio).expanduser()
        if not emotion_audio or not emo_audio_path.exists():
            return _error(job_id, "EMOTION_AUDIO_INVALID", "情感参考音频不可用,请重新选择。", str(emotion_audio), True)
        emotion_path = emo_audio_path
    elif control_mode == "emotext":
        if not emotion_text.strip():
            return _error(job_id, "EMOTION_TEXT_EMPTY", "请输入情感描述文本。", "empty emotion text", True)
        emotion_text_arg = emotion_text
    else:
        emotion_vector = [float(value) for value in vector]

    if len(text_segments) > 1:
        return _run_segmented_in_process(
            job_id=job_id,
            started=started,
            vendor_dir=vendor_dir,
            model_dir=model_dir,
            reference_audio=reference_audio,
            temp_wav=temp_wav,
            text_segments=text_segments,
            device=device,
            emotion_path=emotion_path,
            emotion_text=emotion_text_arg,
            emotion_vector=emotion_vector,
            emotion_weight=weight,
            emotion=emotion,
            model_version=str(payload.get('model_version', '2.0')),
            speech=payload.get('speech'),
        )

    return _run_single_in_process(
        job_id=job_id,
        started=started,
        vendor_dir=vendor_dir,
        model_dir=model_dir,
        reference_audio=reference_audio,
        temp_wav=temp_wav,
        text=text,
        device=device,
        emotion_path=emotion_path,
        emotion_text=emotion_text_arg,
        emotion_vector=emotion_vector,
        emotion_weight=weight,
        emotion=emotion,
        model_version=str(payload.get('model_version', '2.0')),
        speech=payload.get('speech'),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args(argv)
    if args.server:
        return run_server()
    if not args.request_json:
        parser.error("--request-json is required unless --server is used")
    return run_request(Path(args.request_json).expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
