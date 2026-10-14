#!/usr/bin/env python3
"""Prepare explicit owner-bound PyInstaller roots for one Phase 10 candidate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat


_OWNER = "lulu.phase10-build-root/v1"
_MARKER = ".lulu-phase10-root.json"
_IDENTITIES = frozenset(
    {"candidate", "trusted-test", "trusted-test-safe-e2e", "production"}
)


class BuildRootContractError(RuntimeError):
    """The requested build roots are ambiguous, unsafe, or foreign."""


def _absolute(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise BuildRootContractError(f"{label}必须是无..的显式绝对路径")
    return value


def _existing_directory(path: Path, label: str) -> os.stat_result:
    value = _absolute(path, label)
    current = Path(value.anchor)
    try:
        for component in value.parts[1:]:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BuildRootContractError(f"{label}祖先禁止符号链接")
    except BuildRootContractError:
        raise
    except OSError as exc:
        raise BuildRootContractError(f"{label}不可用") from exc
    metadata = value.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise BuildRootContractError(f"{label}owner、mode或类型无效")
    return metadata


def _ensure_base(app_root: Path, role: str) -> Path:
    base = app_root / role
    try:
        metadata = base.lstat()
    except FileNotFoundError:
        try:
            base.mkdir(mode=0o700)
        except OSError as exc:
            raise BuildRootContractError(f"{role}父目录无法创建") from exc
    except OSError as exc:
        raise BuildRootContractError(f"{role}父目录不可用") from exc
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise BuildRootContractError(f"{role}父目录禁止符号链接")
    _existing_directory(base, f"{role}父目录")
    return base


def _marker_payload(
    *,
    app_root: Path,
    root: Path,
    role: str,
    identity: str,
    version: str,
    build: str,
) -> dict[str, object]:
    return {
        "schema_id": "lulu.phase10-build-root/v1",
        "schema_revision": 1,
        "owner": _OWNER,
        "role": role,
        "workspace_app": str(app_root),
        "root_path": str(root),
        "candidate_identity": {
            "identity": identity,
            "version": version,
            "build": build,
        },
    }


def _inspect_root(root: Path, expected: dict[str, object], label: str) -> str:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise BuildRootContractError(f"{label}不可用") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise BuildRootContractError(f"{label}owner、mode或类型无效")
    entries = tuple(root.iterdir())
    if not entries:
        return "empty"
    marker = root / _MARKER
    try:
        marker_metadata = marker.lstat()
        if (
            stat.S_ISLNK(marker_metadata.st_mode)
            or not stat.S_ISREG(marker_metadata.st_mode)
            or marker_metadata.st_uid != os.getuid()
            or marker_metadata.st_mode & 0o022
        ):
            raise BuildRootContractError(f"{label}归属标记无效")
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except BuildRootContractError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BuildRootContractError(f"{label}非空且不属于本候选") from exc
    if payload != expected:
        raise BuildRootContractError(f"{label}非空且不属于本候选")
    return "owned"


def _write_marker(root: Path, payload: dict[str, object]) -> None:
    marker = root / _MARKER
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("root marker short write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise BuildRootContractError("候选root归属标记写入失败") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def prepare_phase10_build_roots(
    *,
    app_root: Path,
    build_root: Path,
    dist_root: Path,
    identity: str,
    version: str,
    build: str,
) -> dict[str, object]:
    """Validate both roots before creating owner markers; never accepts foreign content."""

    app = _absolute(app_root, "workspace app")
    _existing_directory(app, "workspace app")
    build_path = _absolute(build_root, "build root")
    dist_path = _absolute(dist_root, "dist root")
    if build_path == dist_path:
        raise BuildRootContractError("build root与dist root不能相同")
    if identity not in _IDENTITIES or not version or not build:
        raise BuildRootContractError("候选身份无效")
    expected_leaf = f"phase10-{identity}-{build}"
    build_base = _ensure_base(app, "build")
    dist_base = _ensure_base(app, "dist")
    if build_path.parent != build_base or build_path.name != expected_leaf:
        raise BuildRootContractError("build root必须是workspace/app/build下的精确候选root")
    if dist_path.parent != dist_base or dist_path.name != expected_leaf:
        raise BuildRootContractError("dist root必须是workspace/app/dist下的精确候选root")

    specifications = []
    for role, root in (("build", build_path), ("dist", dist_path)):
        payload = _marker_payload(
            app_root=app,
            root=root,
            role=role,
            identity=identity,
            version=version,
            build=build,
        )
        state = _inspect_root(root, payload, f"{role} root")
        specifications.append((root, payload, state))

    for root, payload, state in specifications:
        if state == "absent":
            try:
                root.mkdir(mode=0o700)
            except OSError as exc:
                raise BuildRootContractError("候选root创建失败") from exc
            _existing_directory(root, "候选root")
            _write_marker(root, payload)
        elif state == "empty":
            _write_marker(root, payload)

    return {
        "schema_id": "lulu.phase10-build-roots/v1",
        "owner": _OWNER,
        "build_root": str(build_path),
        "dist_root": str(dist_path),
        "identity": identity,
        "version": version,
        "build": build,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--build-root", required=True)
    parser.add_argument("--dist-root", required=True)
    parser.add_argument("--identity", required=True, choices=sorted(_IDENTITIES))
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    args = parser.parse_args(argv)
    result = prepare_phase10_build_roots(
        app_root=Path(args.app_root),
        build_root=Path(args.build_root),
        dist_root=Path(args.dist_root),
        identity=args.identity,
        version=args.version,
        build=args.build,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
