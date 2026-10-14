"""Segment planning for IndexTTS2 single long-text synthesis."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .indextts2_text_guard import RISKY_CHAR_LIMIT, count_text_chars

MAX_SEGMENT_CHARS = RISKY_CHAR_LIMIT
MAX_SEGMENTS = 12
MAX_TOTAL_CHARS = MAX_SEGMENT_CHARS * MAX_SEGMENTS

_PIECE_RE = re.compile(r"(?:<[^|>\n]+\|[^>\n]+>|[^。！？!?；;，,、：:.\n])+(?:[。！？!?；;，,、：:.\n]+|$)|[。！？!?；;，,、：:.\n]+")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class SegmentPlan:
    level: str
    char_count: int
    segments: tuple[str, ...] = ()
    max_segment_chars: int = MAX_SEGMENT_CHARS
    max_segments: int = MAX_SEGMENTS
    label: str = ""
    hint: str = ""
    blocking_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.level in {"empty", "too_long"}

    @property
    def is_segmented(self) -> bool:
        return self.level == "segmented" and len(self.segments) > 1


def _normalize_text(text: str) -> str:
    lines = [_SPACE_RE.sub(" ", line).strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _hard_split(piece: str, max_chars: int) -> list[str]:
    chunks, buf, size = [], '', 0
    for unit in re.findall(r'<[^|>\n]+\|[^>\n]+>|.', piece, re.DOTALL):
        count = count_text_chars(unit)
        if buf and size + count > max_chars:
            chunks.append(buf.strip())
            buf, size = '', 0
        buf += unit
        size += count
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def _join_piece(buf: str, piece: str) -> str:
    if not buf:
        return piece
    return f"{buf}{piece}"


def split_text_for_indextts2(text: str, *, max_chars: int = MAX_SEGMENT_CHARS) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    raw_pieces = [m.group(0) for m in _PIECE_RE.finditer(normalized) if m.group(0).strip()]

    out: list[str] = []
    buf = ""
    for piece in raw_pieces:
        if count_text_chars(piece) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_hard_split(piece, max_chars))
            continue
        candidate = _join_piece(buf, piece)
        if count_text_chars(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            buf = piece
    if buf:
        out.append(buf)
    return [seg.strip() for seg in out if seg.strip()]


def assess_segment_plan(text: str) -> SegmentPlan:
    char_count = count_text_chars(text)
    if char_count <= 0:
        return SegmentPlan(
            level="empty",
            char_count=0,
            label="未输入文字",
            hint="请输入要生成的文字。",
            blocking_reason="请输入要生成的文字",
        )
    if char_count > MAX_TOTAL_CHARS:
        return SegmentPlan(
            level="too_long",
            char_count=char_count,
            label="文本过长",
            hint=(
                f"{char_count} 字 · 已超过当前分段生成保护上限 {MAX_TOTAL_CHARS} 字。"
                "本节点只支持单条中长文本最小闭环,不是长文工作室。"
            ),
            blocking_reason=f"文本过长:{char_count} 字,请缩短到 {MAX_TOTAL_CHARS} 字以内",
        )
    segments = split_text_for_indextts2(text)
    if any(count_text_chars(segment) > MAX_SEGMENT_CHARS for segment in segments):
        return SegmentPlan(level="too_long", char_count=char_count, label="读音标注过长",
                           hint="请缩短单条读音标注。", blocking_reason="读音标注过长，请缩短后再生成")
    if len(segments) > MAX_SEGMENTS:
        return SegmentPlan(
            level="too_long",
            char_count=char_count,
            segments=tuple(segments),
            label="段落过多",
            hint=f"预计 {len(segments)} 段 · 已超过当前保护上限 {MAX_SEGMENTS} 段。",
            blocking_reason=f"分段过多:{len(segments)} 段,请缩短文本或减少段落",
        )
    if len(segments) <= 1:
        return SegmentPlan(
            level="single",
            char_count=char_count,
            segments=tuple(segments or [_normalize_text(text)]),
            label="单段生成",
            hint=f"{char_count} 字 · 当前将按单段生成。",
        )
    return SegmentPlan(
        level="segmented",
        char_count=char_count,
        segments=tuple(segments),
        label="分段生成",
        hint=f"{char_count} 字 · 将分为 {len(segments)} 段逐段生成并合并为一条音频。",
    )
