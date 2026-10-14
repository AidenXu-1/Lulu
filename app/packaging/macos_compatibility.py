#!/usr/bin/env python3
"""macOS 15 构建输入与最终 App 原生兼容门禁。"""

from __future__ import annotations

import argparse
from collections import Counter
from importlib import metadata
import json
from pathlib import Path
import plistlib
import re
import stat
import subprocess
from typing import Callable


MACOS_FLOOR = (15, 0)
MACOS_FLOOR_TEXT = "15.0"

EXPECTED_DISTRIBUTIONS = {
    "mlx": {
        "version": "0.31.2",
        "tag": "cp311-cp311-macosx_15_0_arm64",
        "dist_info": "mlx-0.31.2.dist-info",
    },
    "mlx-metal": {
        "version": "0.31.2",
        "tag": "py3-none-macosx_15_0_arm64",
        "dist_info": "mlx_metal-0.31.2.dist-info",
    },
    "mlx-whisper": {
        "version": "0.4.3",
        "tag": "py3-none-any",
        "dist_info": "mlx_whisper-0.4.3.dist-info",
    },
}

_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",  # 32-bit big endian
    b"\xce\xfa\xed\xfe",  # 32-bit little endian
    b"\xfe\xed\xfa\xcf",  # 64-bit big endian
    b"\xcf\xfa\xed\xfe",  # 64-bit little endian
    b"\xca\xfe\xba\xbe",  # universal
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",  # universal 64
    b"\xbf\xba\xfe\xca",
}
_MINOS_PATTERN = re.compile(r"^\s*minos\s+(\d+(?:\.\d+){0,2})\s*$", re.MULTILINE)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = str(value or "").strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"无效版本号:{value!r}")
    return tuple(int(part) for part in parts)


def _version_text(value: tuple[int, ...]) -> str:
    parts = list(value)
    while len(parts) < 2:
        parts.append(0)
    return ".".join(str(part) for part in parts)


def _wheel_tags(wheel_text: str | None) -> list[str]:
    return [
        line.partition(":")[2].strip()
        for line in str(wheel_text or "").splitlines()
        if line.startswith("Tag:") and line.partition(":")[2].strip()
    ]


def evaluate_build_environment(
    *,
    distribution_reader: Callable[[str], object] = metadata.distribution,
) -> dict:
    """校验 PyInstaller 将要读取的环境，不允许构建机自行选择 wheel。"""
    packages: dict[str, dict] = {}
    violations: list[str] = []
    for name, expected in EXPECTED_DISTRIBUTIONS.items():
        try:
            distribution = distribution_reader(name)
            version = str(getattr(distribution, "version", ""))
            tags = _wheel_tags(distribution.read_text("WHEEL"))
        except Exception as exc:  # noqa: BLE001 - 缺包必须失败关闭
            packages[name] = {"present": False, "error": type(exc).__name__}
            violations.append(f"{name} 未安装或元数据不可读")
            continue
        packages[name] = {"present": True, "version": version, "wheel_tags": tags}
        if version != expected["version"]:
            violations.append(f"{name} 版本为 {version}，要求 {expected['version']}")
        if tags != [expected["tag"]]:
            shown = ", ".join(tags) if tags else "缺少 WHEEL tag"
            violations.append(f"{name} WHEEL tag 为 {shown}，要求 {expected['tag']}")
    return {
        "schema": 1,
        "target_macos": MACOS_FLOOR_TEXT,
        "ok": not violations,
        "packages": packages,
        "violations": violations,
    }


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def _default_vtool_runner(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/vtool", "-show-build", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _under_materialized_alias(
    app: Path,
    path: Path,
    materialized_alias_roots: tuple[str, ...],
) -> bool:
    relative = path.relative_to(app).as_posix()
    return any(
        relative == root or relative.startswith(root + "/")
        for root in materialized_alias_roots
    )


def _app_wheel_report(
    app: Path,
    materialized_alias_roots: tuple[str, ...] = (),
) -> tuple[dict[str, dict], list[str]]:
    packages: dict[str, dict] = {}
    violations: list[str] = []
    contents = app / "Contents"
    for name, expected in EXPECTED_DISTRIBUTIONS.items():
        matches = [
            path
            for path in contents.rglob(expected["dist_info"])
            if path.is_dir() and not path.is_symlink()
        ]
        primary = [
            path
            for path in matches
            if not _under_materialized_alias(
                app, path, materialized_alias_roots
            )
        ]
        aliases = [path for path in matches if path not in primary]
        if len(primary) != 1:
            packages[name] = {
                "present": False,
                "matches": len(matches),
                "primary_matches": len(primary),
                "materialized_alias_matches": len(aliases),
            }
            violations.append(
                f"{name} 非别名 dist-info 数量为 {len(primary)}，要求 1"
            )
            continue
        tags_by_path: list[tuple[Path, list[str]]] = []
        for match in matches:
            wheel = match / "WHEEL"
            try:
                wheel_st = wheel.lstat()
                if stat.S_ISLNK(wheel_st.st_mode) or not stat.S_ISREG(wheel_st.st_mode):
                    raise OSError("WHEEL 不是普通文件")
                tags = _wheel_tags(wheel.read_text(encoding="utf-8"))
            except OSError:
                tags = []
            tags_by_path.append((match, tags))
        tags = tags_by_path[0][1] if tags_by_path else []
        packages[name] = {
            "present": True,
            "path": str(primary[0].relative_to(app)),
            "matches": len(matches),
            "materialized_alias_matches": len(aliases),
            "wheel_tags": tags,
        }
        invalid = [
            (path, item_tags)
            for path, item_tags in tags_by_path
            if item_tags != [expected["tag"]]
        ]
        if invalid:
            path, item_tags = invalid[0]
            shown = ", ".join(item_tags) if item_tags else "缺少 WHEEL tag"
            violations.append(
                f"{name} 包内 {path.relative_to(app)} WHEEL tag 为 {shown}，要求 {expected['tag']}"
            )
    return packages, violations


def evaluate_app(
    app: Path,
    *,
    vtool_runner: Callable[[Path], subprocess.CompletedProcess[str]] = _default_vtool_runner,
    materialized_alias_roots: tuple[str, ...] = (),
) -> dict:
    """扫描最终 App 的声明、MLX wheel 与全部 Mach-O 部署目标。"""
    app = Path(app)
    violations: list[str] = []
    try:
        app_st = app.lstat()
    except OSError as exc:
        return {
            "schema": 1,
            "target_macos": MACOS_FLOOR_TEXT,
            "ok": False,
            "violations": [f"App 不可读:{type(exc).__name__}"],
        }
    if stat.S_ISLNK(app_st.st_mode) or not stat.S_ISDIR(app_st.st_mode):
        return {
            "schema": 1,
            "target_macos": MACOS_FLOOR_TEXT,
            "ok": False,
            "violations": ["App 必须是非符号链接目录"],
        }

    info_path = app / "Contents/Info.plist"
    declared_minimum = None
    try:
        info_st = info_path.lstat()
        if stat.S_ISLNK(info_st.st_mode) or not stat.S_ISREG(info_st.st_mode):
            raise OSError("Info.plist 不是普通文件")
        with info_path.open("rb") as handle:
            declared_minimum = str(plistlib.load(handle).get("LSMinimumSystemVersion") or "")
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        violations.append("Info.plist 不可读")
    if declared_minimum != MACOS_FLOOR_TEXT:
        violations.append(
            f"LSMinimumSystemVersion 为 {declared_minimum or '缺失'}，要求 {MACOS_FLOOR_TEXT}"
        )

    packages, wheel_violations = _app_wheel_report(
        app, materialized_alias_roots
    )
    violations.extend(wheel_violations)

    # Check each physical dylib location: MLX resolves its metallib relative
    # to the loaded image, not relative to the Python package directory.
    for library in app.rglob("libmlx.dylib"):
        if library.is_symlink():
            continue
        metal = library.parent / "mlx.metallib"
        try:
            with metal.open("rb") as stream:
                valid = stream.read(4) == b"MTLB"
            valid = valid and metal.resolve().is_relative_to(app.resolve())
        except OSError:
            valid = False
        if not valid:
            violations.append(f"MLX metallib 缺失或无效:{metal.relative_to(app)}")

    minos_counts: Counter[str] = Counter()
    native_violations: list[dict] = []
    native_count = 0
    maximum: tuple[int, ...] | None = None
    for path in sorted(app.rglob("*")):
        try:
            path_st = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(path_st.st_mode) or not stat.S_ISREG(path_st.st_mode) or not _is_macho(path):
            continue
        native_count += 1
        result = vtool_runner(path)
        minos_values = _MINOS_PATTERN.findall(str(result.stdout or "")) if result.returncode == 0 else []
        if not minos_values:
            native_violations.append({
                "path": str(path.relative_to(app)),
                "reason": "无法读取 Mach-O 最低系统版本",
            })
            continue
        for raw_minos in minos_values:
            try:
                parsed = _version_tuple(raw_minos)
            except ValueError:
                native_violations.append({
                    "path": str(path.relative_to(app)),
                    "reason": f"无效 minos:{raw_minos}",
                })
                continue
            normalized = _version_text(parsed)
            minos_counts[normalized] += 1
            if maximum is None or parsed > maximum:
                maximum = parsed
            if parsed > MACOS_FLOOR:
                native_violations.append({
                    "path": str(path.relative_to(app)),
                    "reason": f"minos {normalized} 高于 {MACOS_FLOOR_TEXT}",
                })
    if native_count == 0:
        native_violations.append({"path": None, "reason": "App 内未发现 Mach-O 文件"})
    if native_violations:
        violations.append(f"原生兼容门禁发现 {len(native_violations)} 项问题")

    return {
        "schema": 1,
        "target_macos": MACOS_FLOOR_TEXT,
        "ok": not violations,
        "declared_minimum": declared_minimum,
        "packages": packages,
        "native": {
            "file_count": native_count,
            "slice_count": sum(minos_counts.values()),
            "minos_counts": dict(sorted(minos_counts.items())),
            "maximum_minos": _version_text(maximum) if maximum is not None else None,
            "above_floor_count": sum(
                1 for item in native_violations if "高于" in str(item.get("reason"))
            ),
            "violations": native_violations,
        },
        "violations": violations,
        "materialized_alias_roots": len(materialized_alias_roots),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_env = subparsers.add_parser("verify-build-env")
    build_env.add_argument("--output")
    app_parser = subparsers.add_parser("verify-app")
    app_parser.add_argument("--app", required=True)
    app_parser.add_argument("--output")
    args = parser.parse_args()

    report = (
        evaluate_build_environment()
        if args.command == "verify-build-env"
        else evaluate_app(Path(args.app))
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
