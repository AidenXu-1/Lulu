"""Small helpers for IndexTTS2 generated-result delivery."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

_BAD_FILENAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')
_WHITESPACE = re.compile(r"\s+")
_EMOTION_LABELS = {
    "natural": "自然",
    "happy": "开心",
    "firm": "坚定",
    "gentle": "温柔",
    "excited": "激动",
    "calm": "平静",
}


def clean_filename_part(value: object, *, fallback: str = "未命名", max_chars: int = 28) -> str:
    """Return a short filesystem-safe filename segment."""

    text = _WHITESPACE.sub("_", str(value or "").strip())
    text = _BAD_FILENAME_CHARS.sub("_", text)
    text = text.strip("._ ")
    if not text:
        text = fallback
    if len(text) > max_chars:
        text = text[:max_chars].rstrip("._ ")
    return text or fallback


def default_export_filename(
    *,
    template_name: object,
    voice_name: object,
    generated_at: datetime | None = None,
) -> str:
    stamp = (generated_at or datetime.now()).strftime("%Y%m%d-%H%M")
    template = clean_filename_part(template_name, fallback="情感模板")
    voice = clean_filename_part(voice_name, fallback="音色")
    return f"Lulu_IndexTTS2_{template}_{voice}_{stamp}.wav"


def export_target_path(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if target.suffix.lower() != ".wav":
        target = target.with_suffix(".wav")
    return target


def unique_output_path(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法生成不覆盖已有文件的输出路径。")


def build_result_summary(
    *,
    output_path: str | Path,
    voice_label: str,
    style_meta: dict[str, Any] | None,
    result_meta: dict[str, Any] | None,
    duration_seconds: float | None,
    glossary_applied: bool,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now()
    style = dict(style_meta or {})
    meta = dict(result_meta or {})
    style_name = str(style.get("name") or "自然")
    style_source = str(style.get("source") or "system")
    template_kind = "自定义模板" if style_source == "custom" else "系统模板"
    if style.get("dirty"):
        template_kind = "未保存配置"
    emotion_key = str(meta.get("emotion_preset") or style.get("emotion_preset") or "natural")
    emotion_label = _EMOTION_LABELS.get(emotion_key, emotion_key)
    weight = meta.get("emotion_weight", style.get("emotion_weight", 0.8))
    try:
        weight_label = f"{float(weight):.2f}"
    except (TypeError, ValueError):
        weight_label = "未知"
    emotion_mode = str(meta.get("emotion_mode") or "preset_vector")
    advanced = emotion_mode == "custom_vector"
    advanced_label = "高级向量" if advanced else "模板向量"
    glossary_label = "已应用术语读音" if glossary_applied else "未应用术语读音"
    text_length = dict(meta.get("text_length") or {})
    text_length_label = str(text_length.get("label") or "")
    text_length_part = f" · 文本:{text_length_label}" if text_length_label else ""
    segment_count = int(meta.get("segment_count") or text_length.get("segment_count") or 0)
    segment_generation = bool(meta.get("segment_generation") or text_length.get("segment_generation"))
    segment_label = f"分段生成 · {segment_count} 段完成" if segment_generation and segment_count > 1 else ""
    segment_part = f" · {segment_label}" if segment_label else ""
    duration = f"{float(duration_seconds):.1f}s" if duration_seconds is not None else "未知时长"
    name = default_export_filename(template_name=style_name, voice_name=voice_label, generated_at=generated)
    summary = (
        f"模型:IndexTTS2 · 音色:{voice_label} · 情感模板:{style_name}({template_kind}) · "
        f"情感:{emotion_label} · 权重:{weight_label} · {advanced_label} · {glossary_label} · "
        f"生成时间:{generated.strftime('%Y-%m-%d %H:%M')}{text_length_part}{segment_part}"
    )
    result = {
        "output_path": str(Path(output_path)),
        "model": "IndexTTS2",
        "voice_label": voice_label,
        "style_name": style_name,
        "style_source": style_source,
        "template_kind": template_kind,
        "emotion_label": emotion_label,
        "emotion_weight": weight_label,
        "advanced": advanced,
        "advanced_label": advanced_label,
        "glossary_applied": glossary_applied,
        "glossary_label": glossary_label,
        "generated_at": generated.isoformat(timespec="seconds"),
        "generated_at_label": generated.strftime("%Y-%m-%d %H:%M"),
        "duration_label": duration,
        "text_length": text_length,
        "text_length_label": text_length_label,
        "segment_generation": segment_generation,
        "segment_count": segment_count,
        "segment_label": segment_label,
        "default_filename": name,
        "summary": summary,
        "title": f"{style_name} · {voice_label} · {duration}",
    }
    if str(meta.get('model_version')) in ('2.0', '2.5'):
        modes = {'follow': '跟随音色', 'emoaudio': '参考情绪', 'template': '情感模板',
                 'vector': '八维调节', 'emotext': '文字描述'}
        mode = str(meta.get('emotion_control_mode') or 'follow')
        speech = dict(meta.get('speech') or {})
        language = {'ZH': '中文', 'EN': '英语', 'JA': '日语', 'ES': '西班牙语', 'AR': '阿拉伯语'}.get(speech.get('language'), '中文')
        expression = modes.get(mode, '跟随音色')
        if mode == 'template':
            expression += f'：{style_name}'
        kind = '选段试听' if meta.get('preview') else '配音'
        result.update(model='IndexTTS 2.5',
            title=f'{kind} · {voice_label} · {duration}',
            summary=f"IndexTTS 2.5 · {voice_label} · {language} · {float(speech.get('speed', 1.0)):.2f} 倍 · {expression} · {glossary_label}{segment_part}",
            default_filename=default_export_filename(template_name=kind + '_' + expression,
                voice_name=voice_label, generated_at=generated).replace('Lulu_IndexTTS2_', 'Lulu_IndexTTS2.5_'))
        if str(meta.get('model_version')) == '2.0':
            result.update(model='IndexTTS 2',
                summary=f"IndexTTS 2 · {voice_label} · 中英自动识别 · 原生节奏 · {expression}{segment_part}",
                default_filename=result['default_filename'].replace('Lulu_IndexTTS2.5_', 'Lulu_IndexTTS2_'))
    if str(meta.get('model_version')) in ('2.0', '2.5') and meta.get('output_level', {}).get('applied'):
        result['summary'] += ' · 音量已调整'
    return result
