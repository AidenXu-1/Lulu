"""IndexTTS2 emotion preset mapping.

Kept in the synthesis layer so UI only has to pass a stable preset key.
"""

from __future__ import annotations

EMOTION_PRESETS: dict[str, tuple[float, ...]] = {
    "natural": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "happy": (0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.05, 0.15),
    "firm": (0.0, 0.0, 0.0, 0.45, 0.0, 0.0, 0.2, 0.15),
    "gentle": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.4),
    "excited": (0.55, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.05),
    "calm": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.2),
}
EMOTION_VECTOR_LABELS: tuple[str, ...] = (
    "高兴",
    "愤怒",
    "悲伤",
    "恐惧",
    "反感/厌恶",
    "低落",
    "惊讶",
    "自然/平静",
)
MAX_EMOTION_VECTOR_TOTAL = 0.8

DEFAULT_EMOTION_INTENSITY = "standard"
EMOTION_INTENSITY_WEIGHTS: dict[str, float] = {
    "restrained": 0.45,
    "standard": 0.8,
    "vivid": 1.1,
}
INTENSITY_UNSUPPORTED_PRESETS = {"natural"}


def emotion_vector_for(preset: str) -> tuple[float, ...]:
    key = (preset or "natural").strip().lower()
    if key not in EMOTION_PRESETS:
        raise ValueError(f"未知 IndexTTS2 情感预设:{preset}")
    vector = EMOTION_PRESETS[key]
    if len(vector) != 8 or sum(vector) > MAX_EMOTION_VECTOR_TOTAL:
        raise ValueError(f"IndexTTS2 情感预设不合法:{preset}")
    return vector


def normalize_emotion_vector(vector: object) -> tuple[float, ...]:
    if not isinstance(vector, (list, tuple)):
        raise ValueError("情感向量必须包含 8 个数字。")
    if len(vector) != 8:
        raise ValueError("情感向量必须刚好 8 个数字。")
    values: list[float] = []
    for raw in vector:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("情感向量必须全部是数字。") from exc
        if value < 0 or value > 1:
            raise ValueError("情感向量每项必须在 0.00 到 1.00 之间。")
        values.append(round(value, 4))
    total = sum(values)
    if total > MAX_EMOTION_VECTOR_TOTAL + 1e-6:
        raise ValueError("情感向量总量不能超过 0.80。")
    return tuple(values)


def emotion_vector_total(vector: object) -> float:
    return round(sum(normalize_emotion_vector(vector)), 4)


def supports_emotion_intensity(preset: str) -> bool:
    key = (preset or "natural").strip().lower()
    if key in INTENSITY_UNSUPPORTED_PRESETS:
        return False
    try:
        return sum(emotion_vector_for(key)) > 0
    except ValueError:
        return False


def normalize_emotion_intensity(intensity: str | None, preset: str | None = None) -> str:
    key = (intensity or DEFAULT_EMOTION_INTENSITY).strip().lower()
    if key not in EMOTION_INTENSITY_WEIGHTS:
        key = DEFAULT_EMOTION_INTENSITY
    if preset is not None and not supports_emotion_intensity(preset):
        return DEFAULT_EMOTION_INTENSITY
    return key


def emotion_weight_for(intensity: str | None, preset: str | None = None) -> float:
    return EMOTION_INTENSITY_WEIGHTS[normalize_emotion_intensity(intensity, preset)]


def vector_to_cli(vector: tuple[float, ...]) -> str:
    return ",".join(f"{value:g}" for value in vector)
