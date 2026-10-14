"""Phase 1A 本地文件转录的无 Qt 请求、结果与失败合同。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math
from typing import Literal, TypeAlias

from .models import Segment, Transcript


LocalTranscriptionFailureCode: TypeAlias = Literal[
    "AUTH_REQUIRED",
    "SECURE_STORE_UNAVAILABLE",
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "UNSAFE_PATH",
    "MODEL_NOT_READY",
    "RUNTIME_NOT_READY",
    "CANCELLED",
    "INTERNAL_ERROR",
]


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionRequestV1:
    source_path: str
    engine_name: str = "sensevoice"


@dataclass(frozen=True, slots=True)
class LocalTranscriptionSegmentV1:
    text: str
    start: float | None
    end: float | None


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionResultV1:
    text: str
    segments: tuple[LocalTranscriptionSegmentV1, ...]
    language: str | None
    engine: str
    source_path: str

    def to_transcript(self) -> Transcript:
        return Transcript(
            text=self.text,
            segments=[
                Segment(
                    text=segment.text,
                    start=segment.start,
                    end=segment.end,
                )
                for segment in self.segments
            ],
            language=self.language,
            engine=self.engine,
            source=self.source_path,
            title=None,
            meta={},
            saved_media=None,
        )


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionFailureV1:
    code: LocalTranscriptionFailureCode
    user_message: str


def normalize_local_transcription_progress(
    events: Iterable[tuple[str, float]],
) -> list[tuple[str, float]]:
    """归一化可观测进度，不改写文案也不重排阶段。"""
    normalized: list[tuple[str, float]] = []
    for message, raw_fraction in events:
        if not isinstance(message, str) or not message:
            raise ValueError("progress message 必须是非空字符串")
        if isinstance(raw_fraction, bool) or not isinstance(raw_fraction, (int, float)):
            raise TypeError("progress fraction 必须是数字")
        fraction = float(raw_fraction)
        if not math.isfinite(fraction):
            raise ValueError("progress fraction 必须是有限数")
        if fraction < 0:
            fraction = -1.0
        else:
            fraction = round(min(max(fraction, 0.0), 1.0), 6)
        event = (message, fraction)
        if not normalized or normalized[-1] != event:
            normalized.append(event)
    return normalized
