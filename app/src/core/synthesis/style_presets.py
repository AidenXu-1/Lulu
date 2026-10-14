"""Local product-level style presets for IndexTTS2 synthesis."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import models_manager as mm
from .indextts2_presets import (
    EMOTION_PRESETS,
    emotion_vector_for,
    emotion_weight_for,
    normalize_emotion_intensity,
    normalize_emotion_vector,
)

SCHEMA_VERSION = 3
DEFAULT_STYLE_ID = "system_general"
MAX_NAME_LEN = 24

def _system_style(style_id: str, name: str, emotion: str, intensity: str = "standard") -> dict[str, Any]:
    clean_intensity = normalize_emotion_intensity(intensity, emotion)
    return {
        "id": style_id,
        "name": name,
        "emotion_preset": emotion,
        "emotion_intensity": clean_intensity,
        "emotion_vector": list(emotion_vector_for(emotion)),
        "emotion_weight": emotion_weight_for(clean_intensity, emotion),
        "glossary_apply_enabled": True,
        "system": True,
    }


SYSTEM_STYLES: tuple[dict[str, Any], ...] = (
    _system_style(DEFAULT_STYLE_ID, "自然", "natural"),
    _system_style("system_happy", "开心", "happy"),
    _system_style("system_firm", "坚定", "firm"),
    _system_style("system_gentle", "温柔", "gentle"),
    _system_style("system_excited", "激动", "excited"),
    _system_style("system_calm", "平静", "calm"),
)


def style_presets_path() -> Path:
    return mm.app_data_dir() / "synthesis_style_presets.json"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _default_store() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "last_selected_style_id": DEFAULT_STYLE_ID, "custom_styles": []}


def _valid_emotion(value: str) -> bool:
    return value in EMOTION_PRESETS


def _clean_name(name: str) -> str:
    return " ".join(str(name or "").split())


def _clean_custom(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = _clean_name(str(raw.get("name") or ""))
    emotion = str(raw.get("emotion_preset") or "").strip().lower()
    if not name or not _valid_emotion(emotion):
        return None
    intensity = normalize_emotion_intensity(str(raw.get("emotion_intensity") or "standard"), emotion)
    raw_vector = raw.get("emotion_vector")
    if isinstance(raw_vector, (list, tuple)):
        try:
            vector = normalize_emotion_vector(raw_vector)
        except ValueError:
            vector = emotion_vector_for(emotion)
    else:
        vector = emotion_vector_for(emotion)
    try:
        weight = float(raw.get("emotion_weight", emotion_weight_for(intensity, emotion)))
    except (TypeError, ValueError):
        weight = emotion_weight_for(intensity, emotion)
    weight = round(max(0.3, min(1.1, weight)), 2)
    created = str(raw.get("created_at") or _now())
    updated = str(raw.get("updated_at") or created)
    return {
        "id": str(raw.get("id") or f"style_{uuid.uuid4().hex[:10]}"),
        "name": name[:MAX_NAME_LEN],
        "emotion_preset": emotion,
        "emotion_intensity": intensity,
        "emotion_vector": list(vector),
        "emotion_weight": weight,
        "glossary_apply_enabled": bool(raw.get("glossary_apply_enabled", True)),
        "system": False,
        "created_at": created,
        "updated_at": updated,
    }


def _dedupe_custom(styles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    clean: list[dict[str, Any]] = []
    for item in styles:
        style_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not style_id or style_id in seen_ids or name in seen_names:
            continue
        seen_ids.add(style_id)
        seen_names.add(name)
        clean.append(item)
    return clean


def load_store(path: Path | None = None) -> dict[str, Any]:
    path = path or style_presets_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_store()
    except Exception:
        return {**_default_store(), "load_error": True}

    custom: list[dict[str, Any]] = []
    for item in raw.get("custom_styles") or []:
        if isinstance(item, dict):
            style = _clean_custom(item)
            if style:
                custom.append(style)
    custom = _dedupe_custom(custom)
    style_ids = {style["id"] for style in SYSTEM_STYLES} | {style["id"] for style in custom}
    last_selected = str(raw.get("last_selected_style_id") or DEFAULT_STYLE_ID)
    if last_selected not in style_ids:
        last_selected = DEFAULT_STYLE_ID
    return {
        "schema_version": SCHEMA_VERSION,
        "last_selected_style_id": last_selected,
        "custom_styles": custom,
    }


def save_store(store: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or style_presets_path()
    custom: list[dict[str, Any]] = []
    for item in store.get("custom_styles") or []:
        if isinstance(item, dict):
            style = _clean_custom(item)
            if style:
                custom.append(style)
    custom = _dedupe_custom(custom)
    style_ids = {style["id"] for style in SYSTEM_STYLES} | {style["id"] for style in custom}
    last_selected = str(store.get("last_selected_style_id") or DEFAULT_STYLE_ID)
    if last_selected not in style_ids:
        last_selected = DEFAULT_STYLE_ID
    clean = {
        "schema_version": SCHEMA_VERSION,
        "last_selected_style_id": last_selected,
        "custom_styles": custom,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def list_styles(store: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    store = store if store is not None else load_store()
    return [dict(style) for style in SYSTEM_STYLES] + [dict(style) for style in store.get("custom_styles") or []]


def find_style_by_name(name: str, store: dict[str, Any] | None = None) -> dict[str, Any] | None:
    clean_name = _clean_name(name)
    if not clean_name:
        return None
    for style in list_styles(store):
        if str(style.get("name") or "") == clean_name:
            return dict(style)
    return None


def is_system_style_name(name: str) -> bool:
    return bool(find_style_by_name(name, {"custom_styles": []}))


def get_style(style_id: str | None, store: dict[str, Any] | None = None) -> dict[str, Any]:
    target = style_id or DEFAULT_STYLE_ID
    for style in list_styles(store):
        if style.get("id") == target:
            return dict(style)
    return dict(SYSTEM_STYLES[0])


def last_selected_style(store: dict[str, Any] | None = None) -> dict[str, Any]:
    store = store if store is not None else load_store()
    return get_style(str(store.get("last_selected_style_id") or DEFAULT_STYLE_ID), store)


def validate_style(name: str, emotion_preset: str, emotion_intensity: str | None = None) -> tuple[str, str, str]:
    clean_name = _clean_name(name)
    if not clean_name:
        raise ValueError("请输入模板名称。")
    if len(clean_name) > MAX_NAME_LEN:
        raise ValueError(f"名称过长,建议控制在 {MAX_NAME_LEN} 个字以内。")
    emotion = str(emotion_preset or "").strip().lower()
    if not _valid_emotion(emotion):
        raise ValueError("请选择关联情感预设。")
    return clean_name, emotion, normalize_emotion_intensity(emotion_intensity, emotion)


def upsert_custom_style(
    name: str,
    emotion_preset: str,
    glossary_apply_enabled: bool,
    emotion_intensity: str | None = None,
    *,
    style_id: str | None = None,
    emotion_vector: list[float] | tuple[float, ...] | None = None,
    emotion_weight: float | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    name, emotion, intensity = validate_style(name, emotion_preset, emotion_intensity)
    vector = normalize_emotion_vector(emotion_vector if emotion_vector is not None else emotion_vector_for(emotion))
    try:
        weight = float(emotion_weight if emotion_weight is not None else emotion_weight_for(intensity, emotion))
    except (TypeError, ValueError):
        weight = emotion_weight_for(intensity, emotion)
    weight = round(max(0.3, min(1.1, weight)), 2)
    store = load_store(path)
    custom = list(store.get("custom_styles") or [])
    system_names = {str(style["name"]) for style in SYSTEM_STYLES}
    for style in custom:
        if style["name"] == name and style["id"] != style_id:
            raise ValueError("已有同名模板,请换一个名称后另存。")
    if name in system_names:
        raise ValueError("已有同名系统模板,请换一个名称后另存。")
    now = _now()
    if style_id:
        for style in custom:
            if style["id"] == style_id:
                style.update(
                    {
                        "name": name,
                        "emotion_preset": emotion,
                        "emotion_intensity": intensity,
                        "emotion_vector": list(vector),
                        "emotion_weight": weight,
                        "glossary_apply_enabled": bool(glossary_apply_enabled),
                        "updated_at": now,
                    }
                )
                store["custom_styles"] = custom
                store["last_selected_style_id"] = style_id
                return save_store(store, path)
    new_id = style_id or f"style_{uuid.uuid4().hex[:10]}"
    custom.append(
        {
            "id": new_id,
            "name": name,
            "emotion_preset": emotion,
            "emotion_intensity": intensity,
            "emotion_vector": list(vector),
            "emotion_weight": weight,
            "glossary_apply_enabled": bool(glossary_apply_enabled),
            "system": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    store["custom_styles"] = custom
    store["last_selected_style_id"] = new_id
    return save_store(store, path)


def delete_custom_style(style_id: str, path: Path | None = None) -> dict[str, Any]:
    store = load_store(path)
    store["custom_styles"] = [style for style in store.get("custom_styles") or [] if style.get("id") != style_id]
    if store.get("last_selected_style_id") == style_id:
        store["last_selected_style_id"] = DEFAULT_STYLE_ID
    return save_store(store, path)


def set_last_selected(style_id: str, path: Path | None = None) -> dict[str, Any]:
    store = load_store(path)
    if get_style(style_id, store).get("id") != style_id:
        style_id = DEFAULT_STYLE_ID
    store["last_selected_style_id"] = style_id
    return save_store(store, path)


def style_meta(style: dict[str, Any], *, dirty: bool = False) -> dict[str, Any]:
    emotion = str(style.get("emotion_preset") or "natural")
    intensity = normalize_emotion_intensity(str(style.get("emotion_intensity") or "standard"), emotion)
    vector = style.get("emotion_vector")
    if isinstance(vector, (list, tuple)):
        vector = list(normalize_emotion_vector(vector))
    else:
        vector = None
    try:
        weight = float(style.get("emotion_weight", emotion_weight_for(intensity, emotion)))
    except (TypeError, ValueError):
        weight = emotion_weight_for(intensity, emotion)
    return {
        "id": str(style.get("id") or DEFAULT_STYLE_ID),
        "name": str(style.get("name") or "通用自然"),
        "source": "system" if style.get("system", False) else "custom",
        "dirty": bool(dirty),
        "emotion_intensity": intensity,
        "emotion_vector": vector,
        "emotion_weight": round(max(0.3, min(1.1, weight)), 2),
    }
