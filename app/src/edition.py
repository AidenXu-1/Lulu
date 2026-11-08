"""Sealed development edition identity; production defaults stay unchanged."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import stat
import sys

EXPERIENCE = "development-experience"
EXPERIENCE_META = {
    "app_name": "Lulu 开发体验版",
    "artifact_prefix": "Lulu 开发体验版",
    "bundle_id": "com.lulu.transcribe.development-experience",
    "upload_target": "none",
    "entry": "run_external.py",
}


def profile_metadata(base: dict, *, profile: str, edition=None) -> dict:
    if edition is None:
        return dict(base)
    if edition != EXPERIENCE or profile != "external_candidate":
        raise ValueError("未知开发体验版身份")
    return {**base, **EXPERIENCE_META}


def profile_is_experience(payload: dict) -> bool:
    edition = payload.get("edition")
    if edition is None:
        return False
    if (edition != EXPERIENCE or payload.get("profile") != "external_candidate"
            or any(payload.get(k) != v for k, v in EXPERIENCE_META.items())):
        raise RuntimeError("开发体验版身份缺失或不一致")
    return True


@lru_cache(maxsize=1)
def is_experience() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    path = Path(sys.executable).parent.parent / "Resources/build_profile.json"
    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError("构建身份必须是包内普通文件")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("构建身份缺失或损坏") from exc
    return profile_is_experience(payload)


def display_name() -> str:
    return EXPERIENCE_META["app_name"] if is_experience() else "Lulu"


def product_data_root() -> Path:
    home = Path.home()
    name = "Lulu Development Experience" if is_experience() else "Lulu"
    if platform.system() == "Darwin":
        return home / "Library/Application Support" / name
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", home)) / name
    return home / ".local/share" / name


def config_root() -> Path:
    if is_experience():
        return product_data_root() / "preferences"
    return Path.home() / ".本地转录app"


def optional_integration_targets() -> tuple[Path, Path]:
    if is_experience():
        return (Path.home() / ".agents/skills/lulu-experience-agent",
                Path.home() / ".local/bin/lulu-experience")
    return Path.home() / ".agents/skills/lulu-agent", Path.home() / ".local/bin/lulu"


def valid_app_basename(app: Path) -> bool:
    if app.name == "Lulu.app":
        return True
    if app.name != EXPERIENCE_META["artifact_prefix"] + ".app":
        return False
    profile = app / "Contents/Resources/build_profile.json"
    try:
        if profile.is_symlink() or not stat.S_ISREG(profile.lstat().st_mode):
            return False
        return profile_is_experience(json.loads(profile.read_text(encoding="utf-8")))
    except (OSError, ValueError, RuntimeError):
        return False
