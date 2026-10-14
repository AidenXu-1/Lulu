"""Frozen trusted-test entrypoint with build-sealed identity and features."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import stat
import sys
import time

from src.version import __build__, __channel__, __version__


_EXPECTED_PROFILE_STATIC = {
    "schema": 1,
    "profile": "trusted_test",
    "version": __version__,
    "build": __build__,
    "app_name": "Lulu Trusted Test",
    "bundle_id": "com.lulu.transcribe.trusted-test",
    "artifact_prefix": "Lulu-Trusted-Test",
    "upload_target": "none",
    "entry": "run_trusted_test.py",
    "package_surface": "gui_only/v1",
}
_SOURCE_IDENTITY_RE = re.compile(r"^[0-9a-f]{40}$")
_STAGE_ROOT = Path("/private/tmp/lulu-gui-not-visible-20260823.4-A0EC73")
_STAGE_PATH = _STAGE_ROOT / "stages.jsonl"
_STAGE_SCHEMA = "lulu.gui-not-visible-stage/v1"
_DIAGNOSTIC_CONTRACT_ID = "TASK-20260823-A0EC73/gui-not-visible-stage/v1"
_DIAGNOSTIC_BUILD = "20260823.4"
_SUCCESSFUL_STAGE_SEQUENCE = (
    ("bootstrap", "enter"),
    ("profile_gate", "enter"),
    ("profile_gate", "ok"),
    ("trusted_main_import", "enter"),
    ("trusted_main_import", "ok"),
    ("trusted_main_call", "enter"),
    ("trusted_main", "enter"),
    ("feature_gate", "enter"),
    ("feature_gate", "ok"),
    ("qt_import", "enter"),
    ("qt_import", "ok"),
    ("product_import", "enter"),
    ("product_import", "ok"),
    ("qapplication", "enter"),
    ("qapplication", "ok"),
    ("configuration_theme", "enter"),
    ("configuration_theme", "ok"),
    ("activation", "enter"),
    ("activation", "ok"),
    ("main_window", "enter"),
    ("main_window", "ok"),
    ("window_prepare", "enter"),
    ("window_prepare", "ok"),
    ("show_activate", "enter"),
    ("show_activate", "issued"),
    ("titlebar", "enter"),
    ("titlebar", "ok"),
    ("event_loop", "enter"),
    ("event_loop", "return"),
    ("trusted_main_call", "return"),
)
_ERROR_PHASES = frozenset(
    {
        "profile_gate",
        "trusted_main_import",
        "trusted_main_call",
        "feature_gate",
        "qt_import",
        "product_import",
        "qapplication",
        "configuration_theme",
        "activation",
        "main_window",
        "window_prepare",
        "show_activate",
        "titlebar",
        "event_loop",
    }
)


class _StageRecorder:
    """Create the fixed diagnostic JSONL once and enforce its closed state machine."""

    def __init__(self) -> None:
        try:
            root_metadata = _STAGE_ROOT.lstat()
        except OSError as exc:
            raise RuntimeError("诊断阶段目录不可用") from exc
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise RuntimeError("诊断阶段目录身份无效")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(_STAGE_PATH, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("诊断阶段文件身份无效")
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._sequence = 0
        self._successful_index = 0
        self._last: tuple[str, str] | None = None
        self._terminal = False

    def __call__(self, phase: str, event: str) -> None:
        pair = (phase, event)
        if self._terminal or self._descriptor < 0:
            raise RuntimeError("诊断阶段状态机已终止")
        is_success = (
            self._successful_index < len(_SUCCESSFUL_STAGE_SEQUENCE)
            and pair == _SUCCESSFUL_STAGE_SEQUENCE[self._successful_index]
        )
        is_error = (
            event == "error"
            and phase in _ERROR_PHASES
            and self._last == (phase, "enter")
        )
        if not is_success and not is_error:
            raise RuntimeError("诊断阶段状态转换无效")
        payload = {
            "schema_id": _STAGE_SCHEMA,
            "diagnostic_contract_id": _DIAGNOSTIC_CONTRACT_ID,
            "build": _DIAGNOSTIC_BUILD,
            "pid": os.getpid(),
            "sequence": self._sequence,
            "phase": phase,
            "event": event,
            "wall_time_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
        }
        raw = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        offset = 0
        while offset < len(raw):
            written = os.write(self._descriptor, raw[offset:])
            if written <= 0:
                raise OSError("诊断阶段记录短写")
            offset += written
        os.fsync(self._descriptor)
        self._sequence += 1
        self._last = pair
        if is_error:
            self._terminal = True
        else:
            self._successful_index += 1

    def can_record_error(self, phase: str) -> bool:
        return (
            not self._terminal
            and self._descriptor >= 0
            and phase in _ERROR_PHASES
            and self._last == (phase, "enter")
        )

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


def _record_error_best_effort(recorder: _StageRecorder, phase: str) -> None:
    """Preserve an active business exception if its diagnostic write also fails."""
    try:
        if recorder.can_record_error(phase):
            recorder(phase, "error")
    except BaseException:
        pass


def _read_regular_json(path: Path, label: str) -> dict:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(f"{label}缺失或损坏") from exc
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise RuntimeError(f"{label}必须是包内普通文件")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{label}缺失或损坏") from exc
    if type(payload) is not dict:
        raise RuntimeError(f"{label}格式无效")
    return payload


def _resource_root() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("可信内测入口只能由冻结构建启动")
    executable = Path(str(getattr(sys, "executable", "")))
    meipass = Path(str(getattr(sys, "_MEIPASS", "")))
    if not executable.is_absolute() or not meipass.is_absolute():
        raise RuntimeError("冻结资源根缺失")
    try:
        executable_mode = executable.lstat().st_mode
        contents = executable.parent.parent
        frameworks = contents / "Frameworks"
        resources = contents / "Resources"
        resources_mode = resources.lstat().st_mode
        if (
            not stat.S_ISREG(executable_mode)
            or executable.is_symlink()
            or not stat.S_ISDIR(resources_mode)
            or resources.is_symlink()
            or meipass.resolve(strict=True) != frameworks.resolve(strict=True)
        ):
            raise RuntimeError("冻结资源根与App包结构不一致")
    except OSError as exc:
        raise RuntimeError("冻结资源根与App包结构不一致") from exc
    return resources


def _assert_packaged_profile() -> dict:
    if __channel__ != "trusted-test":
        raise RuntimeError("运行身份与可信内测入口不一致")
    root = _resource_root()
    profile_path = root / "build_profile.json"
    payload = _read_regular_json(profile_path, "构建身份")
    expected_keys = set(_EXPECTED_PROFILE_STATIC) | {
        "source_commit",
        "source_tree",
        "product_source_sha256",
    }
    source_commit = payload.get("source_commit")
    source_tree = payload.get("source_tree")
    product_source_digest = payload.get("product_source_sha256")
    static_payload = {key: payload.get(key) for key in _EXPECTED_PROFILE_STATIC}
    if (
        set(payload) != expected_keys
        or static_payload != _EXPECTED_PROFILE_STATIC
        or not _SOURCE_IDENTITY_RE.fullmatch(str(source_commit))
        or not _SOURCE_IDENTITY_RE.fullmatch(str(source_tree))
        or not re.fullmatch(r"[0-9a-f]{64}", str(product_source_digest))
    ):
        raise RuntimeError("构建身份与可信内测入口不一致")
    authorship = _read_regular_json(root / "AUTHORSHIP.json", "作者归属")
    product_source_path = root / "product_source.json"
    product_source = _read_regular_json(product_source_path, "产品源码封印")
    profile_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    actual_product_source_digest = hashlib.sha256(
        json.dumps(
            product_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        authorship.get("build_profile") != "trusted_test"
        or authorship.get("build_profile_sha256") != profile_sha
        or authorship.get("source_commit") != source_commit
        or authorship.get("source_tree") != source_tree
        or authorship.get("product_source_sha256") != product_source_digest
        or actual_product_source_digest != product_source_digest
        or product_source.get("profile") != "trusted_test"
        or product_source.get("source_commit") != source_commit
        or product_source.get("source_tree") != source_tree
        or product_source.get("entry_module") != "run_trusted_test"
    ):
        raise RuntimeError("构建来源与作者归属不一致")

    from src.release_features import load_manifest

    return load_manifest(
        root / "release_features.json",
        expected_profile="trusted_test",
    )


def main() -> int:
    recorder = _StageRecorder()
    try:
        recorder("bootstrap", "enter")
        recorder("profile_gate", "enter")
        try:
            _assert_packaged_profile()
        except BaseException:
            _record_error_best_effort(recorder, "profile_gate")
            raise
        recorder("profile_gate", "ok")

        recorder("trusted_main_import", "enter")
        try:
            from src.trusted_main import main as trusted_main
        except BaseException:
            _record_error_best_effort(recorder, "trusted_main_import")
            raise
        recorder("trusted_main_import", "ok")

        recorder("trusted_main_call", "enter")
        try:
            result = trusted_main(recorder)
        except BaseException:
            _record_error_best_effort(recorder, "trusted_main_call")
            raise
        recorder("trusted_main_call", "return")
        return result
    finally:
        recorder.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
