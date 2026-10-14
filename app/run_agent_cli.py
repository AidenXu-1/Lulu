"""Frozen console shim for Lulu's source-only Agent CLI product adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys


_ALLOWED_PROFILES = frozenset({"external_candidate", "trusted_test"})
_PACKAGED_PROFILE_LINK_TARGET = "../Resources/build_profile.json"
_PROFILE_TRUST_ERROR = "构建身份必须是当前用户可信的包内普通文件"


def _assert_no_symlink_ancestry(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise RuntimeError(_PROFILE_TRUST_ERROR)


def _trusted_profile_path(root: Path) -> Path:
    root_metadata = root.lstat()
    _assert_no_symlink_ancestry(root)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError(_PROFILE_TRUST_ERROR)

    path = root / "build_profile.json"
    metadata = path.lstat()
    if stat.S_ISREG(metadata.st_mode):
        trusted_path = path
        trusted_metadata = metadata
    elif stat.S_ISLNK(metadata.st_mode):
        contents = root.parent
        bundle = contents.parent
        if (
            root.name != "Frameworks"
            or contents.name != "Contents"
            or bundle.suffix != ".app"
            or os.readlink(path) != _PACKAGED_PROFILE_LINK_TARGET
        ):
            raise RuntimeError(_PROFILE_TRUST_ERROR)
        trusted_path = contents / "Resources" / "build_profile.json"
        _assert_no_symlink_ancestry(trusted_path.parent)
        trusted_metadata = trusted_path.lstat()
        if (
            not stat.S_ISREG(trusted_metadata.st_mode)
            or path.resolve(strict=True) != trusted_path
        ):
            raise RuntimeError(_PROFILE_TRUST_ERROR)
    else:
        raise RuntimeError(_PROFILE_TRUST_ERROR)

    if trusted_metadata.st_uid != os.getuid() or trusted_metadata.st_mode & 0o022:
        raise RuntimeError(_PROFILE_TRUST_ERROR)
    return trusted_path


def _assert_packaged_profile(*, resource_root: Path | None = None) -> dict[str, object]:
    """Read the sealed package profile before importing the product adapter."""

    if resource_root is None:
        if not getattr(sys, "frozen", False):
            raise RuntimeError("Agent CLI产品入口只能由冻结构建启动")
        resource_root = Path(str(getattr(sys, "_MEIPASS", "")))
    root = Path(resource_root)
    if not root.is_absolute():
        raise RuntimeError("冻结资源根必须是绝对路径")
    try:
        trusted_path = _trusted_profile_path(root)
        payload = json.loads(trusted_path.read_text(encoding="utf-8"))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("构建身份缺失或损坏") from exc
    if (
        type(payload) is not dict
        or payload.get("schema") != 1
        or payload.get("profile") not in _ALLOWED_PROFILES
    ):
        raise RuntimeError("构建身份不允许启动Agent CLI产品入口")
    return payload


def main(argv: list[str] | None = None) -> int:
    _assert_packaged_profile()
    from src.agent_cli.product_entry import main as product_main

    frozen_root = Path(str(getattr(sys, "_MEIPASS", "")))
    product_resource_root = _trusted_profile_path(frozen_root).parent
    return product_main(argv, resource_root=product_resource_root)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
