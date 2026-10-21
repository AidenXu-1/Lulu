"""Small shared presentation helpers; destination templates stay with callers."""
import math


def speaker_text(segment) -> str:
    speaker = getattr(segment, 'speaker_id', None)
    prefix = f'发言人{speaker}：' if type(speaker) is int and speaker > 0 else ''
    return prefix + (segment.text or '').strip()


def valid_interval(segment) -> bool:
    a, b = segment.start, segment.end
    return (type(a) in (int, float) and type(b) in (int, float)
            and math.isfinite(a) and math.isfinite(b) and 0 <= a < b)
