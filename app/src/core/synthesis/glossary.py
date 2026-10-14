"""Local TTS glossary for pronunciation-friendly text preprocessing."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import models_manager as mm

SCHEMA_VERSION = 2
_ANNOTATION_RE = re.compile(r'<[^|>\n]+\|[^>\n]+>')
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PATH_RE = re.compile(r"(?:/[^\s，。；;：:]+){2,}|[A-Za-z]:\\[^\s，。；;：:]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]")
# Same syllable grammar as the pinned IndexTTS 2 TextNormalizer. This checks
# notation, not pronunciation quality or support for every possible syllable.
_INDEX2_PINYIN_RE = re.compile(
    r"((?:[bpmfdtnlgkhjqxzcsryw]|[zcs]h)?(?:[aeiouüv]|[ae]i|u[aio]|ao|ou|i[aue]|[uüv]e|[uvü]ang?|uai|[aeiuv]n|[aeio]ng|ia[no]|i[ao]ng)|ng|er)[1-5]",
    re.IGNORECASE,
)


def index2_pinyin(reading: str) -> str | None:
    syllables = reading.strip().split()
    if not syllables or not all(_INDEX2_PINYIN_RE.fullmatch(item) for item in syllables):
        return None
    return " ".join(syllables).upper()


@dataclass(frozen=True)
class GlossaryApplyResult:
    text: str
    applied: bool
    matched_terms: int
    replacement_count: int
    warnings: tuple[str, ...] = ()


def glossary_path() -> Path:
    return mm.app_data_dir() / "synthesis_glossary.json"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _default_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "apply_enabled": True, "terms": []}


def _clean_term(raw: dict[str, Any], order: int) -> dict[str, Any] | None:
    source = str(raw.get("source") or "").strip()
    replacement = str(raw.get("replacement") or "").strip()
    if not source or not replacement:
        return None
    created = str(raw.get("created_at") or _now())
    updated = str(raw.get("updated_at") or created)
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "source": source[:80],
        "replacement": replacement[:120],
        "kind": "pronunciation" if raw.get("kind") == "pronunciation" else "replacement",
        "enabled": bool(raw.get("enabled", True)),
        "order": int(raw.get("order", order)),
        "created_at": created,
        "updated_at": updated,
    }


def load_store(path: Path | None = None) -> dict[str, Any]:
    path = path or glossary_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_store()
    except Exception:
        return {**_default_store(), "load_error": True}
    if not isinstance(raw, dict) or not isinstance(raw.get("terms", []), list):
        return {**_default_store(), "load_error": True}
    terms: list[dict[str, Any]] = []
    for idx, item in enumerate(raw.get("terms") or [], start=1):
        if isinstance(item, dict):
            try:
                term = _clean_term(item, idx)
            except (TypeError, ValueError):
                return {**_default_store(), "load_error": True}
            if term:
                terms.append(term)
    terms.sort(key=lambda item: int(item.get("order", 0)))
    return {
        "schema_version": SCHEMA_VERSION,
        "apply_enabled": bool(raw.get("apply_enabled", True)),
        "terms": terms,
    }


def save_store(store: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or glossary_path()
    if store.get("load_error"):
        raise ValueError("读音词典读取异常，已保留原文件；请先检查词典文件，避免覆盖旧记录。")
    terms: list[dict[str, Any]] = []
    for idx, item in enumerate(store.get("terms") or [], start=1):
        if isinstance(item, dict):
            term = _clean_term(item, idx)
            if term:
                term["order"] = idx
                terms.append(term)
    clean = {
        "schema_version": SCHEMA_VERSION,
        "apply_enabled": bool(store.get("apply_enabled", True)),
        "terms": terms,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return clean


def upsert_term(source: str, replacement: str, *, term_id: str | None = None, enabled: bool = True,
                kind: str = "replacement") -> dict[str, Any]:
    source = source.strip()
    replacement = replacement.strip()
    if not source:
        raise ValueError("请输入原词。")
    if not replacement:
        raise ValueError("请输入读作。")
    if len(source) > 80:
        raise ValueError("原词过长,建议只填写需要纠正的具体词语。")
    if len(replacement) > 120:
        raise ValueError("读作过长,可能影响生成效果。")
    if kind not in {"replacement", "pronunciation"}:
        raise ValueError("请选择文字替换或发音标注。")
    if kind == "pronunciation" and any(char in source + replacement for char in '<>|\n\r'):
        raise ValueError("发音标注只填写原词和读音，不要填写括号、竖线或换行。")
    store = load_store()
    terms = list(store.get("terms") or [])
    for term in terms:
        if term["source"] == source and term["id"] != term_id:
            raise ValueError("已有相同原词,请编辑原词或停用旧词条。")
    now = _now()
    if term_id:
        for term in terms:
            if term["id"] == term_id:
                term.update({"source": source, "replacement": replacement, "enabled": enabled, "updated_at": now, "kind": kind})
                store["terms"] = terms
                return save_store(store)
    terms.append(
        {
            "id": term_id or uuid.uuid4().hex[:12],
            "source": source,
            "replacement": replacement,
            "kind": kind,
            "enabled": enabled,
            "order": len(terms) + 1,
            "created_at": now,
            "updated_at": now,
        }
    )
    store["terms"] = terms
    return save_store(store)


def delete_term(term_id: str) -> dict[str, Any]:
    store = load_store()
    store["terms"] = [term for term in store.get("terms") or [] if term.get("id") != term_id]
    return save_store(store)


def set_term_enabled(term_id: str, enabled: bool) -> dict[str, Any]:
    store = load_store()
    for term in store.get("terms") or []:
        if term.get("id") == term_id:
            term["enabled"] = bool(enabled)
            term["updated_at"] = _now()
            break
    return save_store(store)


def set_apply_enabled(enabled: bool) -> dict[str, Any]:
    store = load_store()
    store["apply_enabled"] = bool(enabled)
    return save_store(store)


def _skip_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in (_URL_RE, _EMAIL_RE, _PATH_RE, _ANNOTATION_RE):
        spans.extend((m.start(), m.end()) for m in pat.finditer(text))
    spans.sort()
    return spans


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _ascii_boundary_ok(text: str, start: int, end: int, source: str) -> bool:
    if not _ASCII_WORD_RE.search(source):
        return True
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not _ASCII_WORD_RE.match(before) and not _ASCII_WORD_RE.match(after)


def apply_glossary(text: str, store: dict[str, Any] | None = None, *, enabled: bool | None = None,
                   pronunciation: bool = False, model_version: str = "2.5") -> GlossaryApplyResult:
    original = text or ""
    store = store if store is not None else load_store()
    if store.get('load_error'):
        return GlossaryApplyResult(original, False, 0, 0, ('glossary_unreadable',))
    apply_enabled = bool(store.get("apply_enabled", True)) if enabled is None else bool(enabled)
    if not apply_enabled:
        return GlossaryApplyResult(original, False, 0, 0)
    terms = [term for term in (store.get("terms") or []) if term.get("enabled", True)]
    if not terms or not original:
        return GlossaryApplyResult(original, False, 0, 0)

    skip_spans = _skip_spans(original)
    matches: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = []
    warnings: set[str] = set()
    for term in sorted(terms, key=lambda item: len(str(item.get("source") or "")), reverse=True):
        source = str(term.get("source") or "")
        replacement = str(term.get("replacement") or "")
        if not source or not replacement:
            continue
        if term.get("kind") == "pronunciation":
            if not pronunciation:
                if source in original:
                    warnings.add("pronunciation_unavailable")
                continue
            if any(char in source + replacement for char in '<>|\n\r'):
                warnings.add("pronunciation_invalid")
                continue
            if model_version == "2.0":
                reading = index2_pinyin(replacement)
                if reading is None:
                    if source in original:
                        warnings.add("pronunciation_unavailable")
                    continue
                replacement = reading
            else:
                replacement = f"<{source}|{replacement}>"
        start = 0
        while True:
            idx = original.find(source, start)
            if idx < 0:
                break
            end = idx + len(source)
            if (
                not _overlaps(idx, end, skip_spans)
                and not _overlaps(idx, end, occupied)
                and _ascii_boundary_ok(original, idx, end, source)
            ):
                matches.append((idx, end, source, replacement))
                occupied.append((idx, end))
            start = end

    if not matches:
        return GlossaryApplyResult(original, False, 0, 0, tuple(sorted(warnings)))
    matches.sort(key=lambda item: item[0])
    pieces: list[str] = []
    cursor = 0
    used_terms: set[str] = set()
    for start, end, source, replacement in matches:
        pieces.append(original[cursor:start])
        pieces.append(replacement)
        cursor = end
        used_terms.add(source)
    pieces.append(original[cursor:])
    return GlossaryApplyResult("".join(pieces), True, len(used_terms), len(matches), tuple(sorted(warnings)))
