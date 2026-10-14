"""Rebuildable, read-only library projection shared by GUI and CLI.

Files remain authoritative. Cache changed bodies once, keep search out of Qt's
event loop, and never synchronize or create Markdown while listing documents.
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from . import library
from .document_metrics import visible_characters

_lock = threading.RLock()
_cache: dict[tuple[str, str], tuple[tuple, dict, str]] = {}


def _timestamp(value):
    try:
        # Legacy timestamps are local; newer receipts use explicit UTC offsets.
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,OverflowError,OSError):
        return float('-inf')


def _stamp(path: Path):
    try:
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    except OSError:
        return None


def query(*, kind="all", search="", folder_id=None, sort="updated", offset=0, limit=200,
          refresh=True) -> dict:
    if (kind not in {"all", "documents", "transcript", "rewrite", "voice"}
            or sort not in {"updated", "created", "title"}
            or type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= 500 or not isinstance(search, str)):
        raise ValueError("资料查询参数无效")
    root = str(library.LIBRARY_DIR)
    kinds = {"transcript", "rewrite"} if kind == "documents" else {kind}
    errors = []
    with _lock:
        if refresh or not any(k[0] == root for k in _cache):
            present = set()
            indexed_ids = set()
            for path in library.LIBRARY_DIR.glob("*.json"):
                if path.name.startswith(".") or path.name == library.FOLDERS_FILE.name:
                    continue
                key = (root, path.stem)
                present.add(key)
                old = _cache.get(key)
                raw_stamp = _stamp(path)
                raw = old[1] if old and old[0][0] == raw_stamp else library._load_raw(path.stem)
                if raw is None:
                    errors.append(path.name)
                    _cache.pop(key, None)
                    continue
                indexed_ids.add(path.stem)
                if raw.get("kind", "transcript") not in {"transcript", "rewrite", "voice"}:
                    _cache.pop(key, None)
                    continue
                voice = raw.get("kind") == "voice"
                body_path = (Path(str(raw.get("audio_path") or "")).expanduser() if voice
                             else library._markdown_abs(raw.get("markdown_path"), path.stem))
                source_id = raw.get("source_id")
                source_stamp = _stamp(library.LIBRARY_DIR / f"{source_id}.json") if voice and source_id else None
                stamp = (raw_stamp, _stamp(body_path), source_stamp)
                if old and stamp == old[0]:
                    continue
                # A cached row contains no full text; reload only the changed record.
                raw = library._load_raw(path.stem)
                if raw is None:
                    errors.append(path.name)
                    _cache.pop(key, None)
                    continue
                text = raw.get("text", "")
                if not voice and body_path.is_file():
                    try:
                        text = body_path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        errors.append(path.name)
                row = {k: v for k, v in raw.items() if k not in {
                    "text", "segments", "source_text_snapshot", "summary", "meta"}}
                row.update(id=path.stem, kind=raw.get("kind", "transcript"),
                           text_preview=library._text_preview(text), char_count=len(text),
                           display_char_count=visible_characters(text),
                           has_summary=bool(raw.get("summary")),
                           source_url=library.reliable_source_url(raw))
                meta = raw.get("meta") or {}
                for field in ("uploader", "author", "stats", "duration"):
                    if field in meta:
                        row[field] = meta[field]
                if voice:
                    row["model_version"] = meta.get("model_version")
                    row["missing_flags"] = {"audio_missing": not body_path.is_file(),
                        "source_missing": bool(raw.get("source_id") and not
                            (library.LIBRARY_DIR / f'{raw["source_id"]}.json').is_file())}
                _cache[key] = stamp, row, "\n".join((str(row.get("title", "")), text,
                    str(row.get("source", "")), str(row.get("voice_name", "")),
                    str(row.get("author", "")), str(row.get("uploader", "")))).casefold()
            from .library_recovery import candidates
            recovered, recovery_errors = candidates(indexed_ids)
            errors.extend(recovery_errors)
            for doc in recovered:
                key = (root, doc['id'])
                present.add(key)
                text = doc['text']
                row = {k: v for k, v in doc.items() if k != 'text'}
                row['text_preview'] = library._text_preview(text)
                # Sentinel forces a real record reload if its index is repaired.
                _cache[key] = (None, None, None), row, (doc['title'] + '\n' + text).casefold()
            for key in list(_cache):
                if key[0] == root and key not in present:
                    del _cache[key]
        needle = search.strip().casefold()
        rows = [dict(row) for (base, _), (_, row, body) in _cache.items()
                if base == root and (kind == "all" or row["kind"] in kinds)
                and (folder_id is None or (row.get("folder_id") or "") == folder_id)
                and (not needle or needle in body)]
        field = {"updated": "updated_at", "created": "created_at", "title": "title"}[sort]
        rows.sort(key=lambda e: ((str(e.get('title') or '').casefold() if sort=='title'
                                 else _timestamp(e.get(field) or e.get('created_at'))), e["id"]),
                  reverse=sort != "title")
        try:
            folders = [dict(f) for f in library._load_folders() if f.get("kind") in kinds]
        except library.FolderIndexError as exc:
            folders = []
            errors.append(str(exc))
        counts = {}
        library_counts = {"documents": 0, "voice": 0}
        for (base, _), (_, row, _) in _cache.items():
            if base == root:
                library_counts["voice" if row["kind"] == "voice" else "documents"] += 1
            if base == root and row["kind"] in kinds:
                fid = row.get("folder_id")
                counts[fid] = counts.get(fid, 0) + 1
        for folder in folders:
            folder["count"] = counts.get(folder["id"], 0)
        folders.sort(key=lambda f: f.get("created_at", ""), reverse=True)
        folders.sort(key=lambda f: f.get("order", float("inf")))
        return {"entries": rows[offset:offset + limit], "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "errors": errors, "folders": folders, "library_counts": library_counts}
