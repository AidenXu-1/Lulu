"""Lulu runtime/build identity: the only source used by app and packaging."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys

__version__ = "4.3.1"
__build__ = "20261022.1"
__channel__ = "production"

_PROFILE_CHANNELS_BY_PARTS = {
    ("internal", "test"): "internal-test",
    ("trusted", "test"): "trusted-test",
    ("external", "candidate"): "production",
}
_PROFILE_IDENTITIES = {
    "trusted_test": {
        "app_name": "Lulu Trusted Test",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "artifact_prefix": "Lulu-Trusted-Test",
        "upload_target": "none",
        "entry": "run_trusted_test.py",
    },
    "external_candidate": {
        "app_name": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "artifact_prefix": "Lulu",
        "upload_target": "github-release-candidate",
        "entry": "run_external.py",
    },
}


def channel_for_profile(profile: str) -> str:
    if not isinstance(profile, str):
        raise RuntimeError("构建身份没有受支持的发布通道")
    profile_parts = tuple(profile.split("_"))
    try:
        return _PROFILE_CHANNELS_BY_PARTS[profile_parts]
    except KeyError as exc:
        raise RuntimeError("构建身份没有受支持的发布通道") from exc


def runtime_channel() -> str:
    """Derive a frozen runtime channel only from its sealed build profile."""

    if not getattr(sys, "frozen", False):
        return __channel__
    executable = Path(str(getattr(sys, "executable", "")))
    if not executable.is_absolute():
        raise RuntimeError("冻结运行身份缺失")
    profile_path = executable.parent.parent / "Resources" / "build_profile.json"
    try:
        mode = profile_path.lstat().st_mode
        if not stat.S_ISREG(mode) or profile_path.is_symlink():
            raise RuntimeError("构建身份必须是包内普通文件")
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("构建身份缺失或损坏") from exc
    if type(payload) is not dict or payload.get("schema") != 1:
        raise RuntimeError("构建身份缺失或损坏")
    profile = payload.get("profile")
    channel = channel_for_profile(profile)
    expected = _PROFILE_IDENTITIES.get(profile)
    if expected is not None and any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("构建身份与发布通道不一致")
    declared_channel = payload.get("channel")
    if declared_channel is not None and declared_channel != channel:
        raise RuntimeError("构建身份与发布通道不一致")
    return channel


def identity() -> dict[str, str]:
    return {"version": __version__, "build": __build__, "channel": runtime_channel()}
