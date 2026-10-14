"""长文本切分。"""

from __future__ import annotations

import re

_SENT_RE = re.compile(r"([^。！？!?；;\n]+[。！？!?；;]?)")


def split_text(text: str, max_chars: int = 120) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    pieces = [m.group(1).strip() for m in _SENT_RE.finditer(text) if m.group(1).strip()]
    if not pieces:
        pieces = [text]

    out: list[str] = []
    buf = ""
    for p in pieces:
        if len(p) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(p[i:i + max_chars] for i in range(0, len(p), max_chars))
            continue
        cand = (buf + " " + p).strip() if buf else p
        if len(cand) <= max_chars:
            buf = cand
        else:
            out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out
