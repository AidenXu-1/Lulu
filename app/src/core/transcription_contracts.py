"""Phase 1A 本地文件转录的无 Qt 请求、结果与失败合同。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
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
    "INCOMPLETE_RESULT",
    "RESOURCE_BUSY",
]


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionRequestV1:
    source_path: str
    engine_name: str = "local_sensevoice_q8"


@dataclass(frozen=True, slots=True)
class LocalTranscriptionSegmentV1:
    text: str
    start: float | None
    end: float | None
    speaker_id: int | None = None


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionResultV1:
    text: str
    segments: tuple[LocalTranscriptionSegmentV1, ...]
    language: str | None
    engine: str
    source_path: str
    asr: dict = field(default_factory=dict)

    def to_transcript(self) -> Transcript:
        return Transcript(
            text=self.text,
            segments=[
                Segment(
                    text=segment.text,
                    start=segment.start,
                    end=segment.end,
                    speaker_id=segment.speaker_id,
                )
                for segment in self.segments
            ],
            language=self.language,
            engine=self.engine,
            source=self.source_path,
            title=None,
            meta={"asr": dict(self.asr)} if self.asr else {},
            saved_media=None,
        )


@dataclass(frozen=True, slots=True)
class LocalFileTranscriptionFailureV1:
    code: LocalTranscriptionFailureCode
    user_message: str


# The Python constructors stay compatible with existing callers. The serialized
# result has an explicit v2 schema; no historical bytes are upgraded in place.
TRANSCRIPTION_SCHEMAS = frozenset({"lulu.transcription-result/v1", "lulu.transcription-result/v2"})


def validate_asr(value: object) -> dict:
    types = {"model_id": str, "model_revision": str, "runtime_version": str,
             "timestamp_kind": str, "diarization": bool, "timing_estimated": bool}
    if type(value) is not dict or set(value) - types.keys():
        raise ValueError("转录模型信息无效")
    for key, item in value.items():
        if type(item) is not types[key] or (type(item) is str and len(item) > 256):
            raise ValueError("转录模型信息无效")
    return dict(value)


def segment_record(segment: Segment | LocalTranscriptionSegmentV1) -> dict:
    return dict(text=segment.text, start=segment.start, end=segment.end, speaker_id=segment.speaker_id)


def result_from_transcript(transcript: Transcript, source_path: str | None = None) -> LocalFileTranscriptionResultV1:
    return LocalFileTranscriptionResultV1(
        transcript.text, tuple(LocalTranscriptionSegmentV1(**segment_record(s)) for s in transcript.segments),
        transcript.language, transcript.engine, source_path if source_path is not None else transcript.source,
        validate_asr((transcript.meta or {}).get("asr", {})))


def encode_transcription_result(job_id: str, result: LocalFileTranscriptionResultV1) -> dict:
    value = dict(text=result.text, segments=[segment_record(s) for s in result.segments],
                 language=result.language, engine=result.engine, asr=validate_asr(result.asr))
    payload = dict(schema_id="lulu.transcription-result/v2", schema_revision=1, job_id=job_id, transcript=value)
    decode_transcription_result(payload, job_id=job_id)
    return payload


def decode_transcription_result(payload: object, *, job_id: str, source: str = "") -> Transcript:
    if (type(payload) is not dict or set(payload) != {"schema_id", "schema_revision", "job_id", "transcript"}
            or payload["schema_id"] not in TRANSCRIPTION_SCHEMAS or type(payload["schema_revision"]) is not int
            or payload["schema_revision"] != 1 or payload["job_id"] != job_id):
        raise ValueError("转录结果身份无效")
    v2 = payload["schema_id"].endswith("/v2")
    value = payload["transcript"]
    fields = {"text", "segments", "language", "engine"} | ({"asr"} if v2 else set())
    if (type(value) is not dict or set(value) != fields or type(value["text"]) is not str
            or type(value["segments"]) is not list or type(value["engine"]) is not str
            or (value["language"] is not None and type(value["language"]) is not str)):
        raise ValueError("转录结果内容无效")
    segments = []
    for row in value["segments"]:
        if (type(row) is not dict or set(row) != ({"text", "start", "end"} | ({"speaker_id"} if v2 else set()))
                or type(row["text"]) is not str):
            raise ValueError("转录分段无效")
        start, end = row["start"], row["end"]
        for timestamp in (start, end):
            if timestamp is not None and (type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0):
                raise ValueError("转录时间无效")
        if v2 and ((start is None) != (end is None) or (start is not None and end < start)):
            raise ValueError("转录时间范围无效")
        speaker = row.get("speaker_id")
        if speaker is not None and (type(speaker) is not int or not 0 < speaker <= 2147483647):
            raise ValueError("转录发言人无效")
        segments.append(Segment(row["text"], start, end, speaker))
    asr = validate_asr(value["asr"]) if v2 else {}
    return Transcript(value["text"], segments, value["language"], value["engine"], source,
                      meta={"asr": asr} if asr else {})


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
