"""Build-sealed release feature contract for the trusted-test channel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


SCHEMA_VERSION = 1
TRUSTED_PROFILE = "trusted_test"
_MAX_MANIFEST_BYTES = 4096
_MANIFEST_NAME = "release_features.json"


def manifest_payload(profile: str = TRUSTED_PROFILE) -> dict:
    if profile != TRUSTED_PROFILE:
        raise ValueError("release feature 清单只允许 trusted_test profile")
    return {
        "schema": SCHEMA_VERSION,
        "profile": TRUSTED_PROFILE,
        "features": {
            "homepage_collection": {
                "xiaohongshu": False,
            },
        },
    }


def validate_manifest(payload: object, *, expected_profile: str = TRUSTED_PROFILE) -> dict:
    if expected_profile != TRUSTED_PROFILE or type(payload) is not dict:
        raise RuntimeError("release feature 清单缺失、损坏或含未授权能力")
    features = payload.get("features")
    homepage = features.get("homepage_collection") if type(features) is dict else None
    valid = (
        set(payload) == {"schema", "profile", "features"}
        and type(payload.get("schema")) is int
        and payload.get("schema") == SCHEMA_VERSION
        and type(payload.get("profile")) is str
        and payload.get("profile") == TRUSTED_PROFILE
        and type(features) is dict
        and set(features) == {"homepage_collection"}
        and type(homepage) is dict
        and set(homepage) == {"xiaohongshu"}
        and type(homepage.get("xiaohongshu")) is bool
        and homepage.get("xiaohongshu") is False
    )
    if not valid:
        raise RuntimeError("release feature 清单缺失、损坏或含未授权能力")
    return manifest_payload(expected_profile)


def _frozen_resources_root() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("release feature 清单只能由冻结构建读取")

    meipass = Path(str(getattr(sys, "_MEIPASS", "")))
    executable = Path(str(getattr(sys, "executable", "")))
    if not meipass.is_absolute() or not executable.is_absolute():
        raise RuntimeError("冻结 Bundle 资源路径异常")

    macos = executable.parent
    contents = macos.parent
    bundle = contents.parent
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    if (
        macos.name != "MacOS"
        or contents.name != "Contents"
        or bundle.suffix != ".app"
        or meipass != frameworks
    ):
        raise RuntimeError("冻结 Bundle 资源路径异常")

    try:
        for path in (bundle, contents, macos, frameworks, resources):
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise RuntimeError("冻结 Bundle 资源路径异常")
        executable_st = executable.lstat()
        if stat.S_ISLNK(executable_st.st_mode) or not stat.S_ISREG(
            executable_st.st_mode
        ):
            raise RuntimeError("冻结 Bundle 可执行文件路径异常")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("冻结 Bundle 资源路径异常") from exc
    return resources


def _manifest_read_path(path: Path) -> Path:
    if not getattr(sys, "frozen", False):
        return path
    meipass = Path(str(getattr(sys, "_MEIPASS", "")))
    resources_manifest = _frozen_resources_root() / _MANIFEST_NAME
    if path not in (meipass / _MANIFEST_NAME, resources_manifest):
        raise RuntimeError("冻结清单只能从 App Bundle Resources 读取")
    return resources_manifest


def load_manifest(path: Path, *, expected_profile: str = TRUSTED_PROFILE) -> dict:
    path = _manifest_read_path(Path(path))
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeError("release feature 清单必须是普通文件")
        if st.st_size <= 0 or st.st_size > _MAX_MANIFEST_BYTES:
            raise RuntimeError("release feature 清单大小异常")
        raw = path.read_bytes()
        if len(raw) != st.st_size:
            raise RuntimeError("release feature 清单读取期间发生变化")
        payload = json.loads(raw.decode("utf-8"))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("release feature 清单缺失或损坏") from exc
    return validate_manifest(payload, expected_profile=expected_profile)


def write_manifest(path: Path, *, profile: str = TRUSTED_PROFILE) -> dict:
    path = Path(path)
    payload = manifest_payload(profile)
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                raise OSError("release feature 清单写入不完整")
            written += count
        os.fsync(fd)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return payload


def sealed_manifest_path() -> Path:
    return _frozen_resources_root() / _MANIFEST_NAME


def load_sealed_features(*, expected_profile: str = TRUSTED_PROFILE) -> dict:
    return load_manifest(
        sealed_manifest_path(),
        expected_profile=expected_profile,
    )


def homepage_collection_enabled(platform: str, *, manifest: dict | None = None) -> bool:
    features = validate_manifest(
        manifest if manifest is not None else load_sealed_features(),
        expected_profile=TRUSTED_PROFILE,
    )
    normalized = str(platform or "").strip().lower()
    if normalized != "xiaohongshu":
        raise KeyError("未声明的平台能力必须失败关闭")
    return features["features"]["homepage_collection"]["xiaohongshu"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create",))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_manifest(Path(args.output), profile=args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
