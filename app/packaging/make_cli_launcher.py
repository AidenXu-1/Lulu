#!/usr/bin/env python3
"""Generate an owner-only, not-yet-install-authorized Lulu CLI launcher candidate."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import stat


_ALLOWED_BUNDLE_IDS = frozenset(
    {"com.lulu.transcribe", "com.lulu.transcribe.trusted-test"}
)
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


class LauncherCandidateError(RuntimeError):
    """Launcher candidate generation failed without authorizing installation."""


def _validate_no_symlink_ancestry(path: Path, label: str) -> None:
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise LauncherCandidateError(f"{label}祖先禁止符号链接")
    except LauncherCandidateError:
        raise
    except OSError as exc:
        raise LauncherCandidateError(f"{label}不可用") from exc


def _validate_owned_path(path: Path, *, directory: bool, label: str) -> os.stat_result:
    if not path.is_absolute():
        raise LauncherCandidateError(f"{label}必须是绝对路径")
    _validate_no_symlink_ancestry(path, label)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LauncherCandidateError(f"{label}不可用") from exc
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_kind(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise LauncherCandidateError(f"{label}owner、mode或类型无效")
    return metadata


def _validate_binding(value: object, label: str, *, maximum: int = 4096) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise LauncherCandidateError(f"{label}绑定无效")
    return value


def generate_launcher_candidate(
    *,
    app_bundle: Path,
    output_path: Path,
    expected_bundle_id: str,
    expected_designated_requirement: str,
    expected_team_id: str,
) -> dict[str, object]:
    """Create a staged candidate only; real signature review must precede install."""

    bundle = Path(app_bundle)
    output = Path(output_path)
    _validate_owned_path(bundle, directory=True, label="Lulu.app")
    if bundle.suffix != ".app":
        raise LauncherCandidateError("Lulu产品包路径无效")
    canonical_cli = bundle / "Contents" / "MacOS" / "lulu-agent-cli"
    cli_metadata = _validate_owned_path(
        canonical_cli,
        directory=False,
        label="Agent CLI",
    )
    if not cli_metadata.st_mode & stat.S_IXUSR:
        raise LauncherCandidateError("Agent CLI缺少owner执行权限")
    if not output.is_absolute() or output.name != "lulu":
        raise LauncherCandidateError("launcher候选必须是显式绝对lulu路径")
    _validate_owned_path(output.parent, directory=True, label="launcher候选目录")
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LauncherCandidateError("launcher候选路径不可用") from exc
    else:
        raise LauncherCandidateError("launcher候选路径已存在，禁止覆盖")

    bundle_id = _validate_binding(expected_bundle_id, "bundle ID", maximum=160)
    if bundle_id not in _ALLOWED_BUNDLE_IDS:
        raise LauncherCandidateError("bundle ID不属于Lulu受控产品")
    requirement = _validate_binding(
        expected_designated_requirement,
        "designated requirement",
    )
    team_id = _validate_binding(expected_team_id, "Team ID", maximum=10)
    if not _TEAM_ID.fullmatch(team_id):
        raise LauncherCandidateError("Team ID格式无效")

    launcher = (
        "#!/bin/sh\n"
        "# lulu.launcher-candidate/v1\n"
        "# install_authorized=false\n"
        f"# expected_bundle_id={bundle_id}\n"
        f"# expected_designated_requirement={requirement}\n"
        f"# expected_team_id={team_id}\n"
        "# real signature and install review are required before installation\n"
        "set -eu\n"
        f"exec {shlex.quote(str(canonical_cli))} \"$@\"\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700,
        )
        created = True
        os.fchmod(descriptor, 0o700)
        offset = 0
        while offset < len(launcher):
            written = os.write(descriptor, launcher[offset:])
            if written <= 0:
                raise OSError("launcher候选短写")
            offset += written
        os.fsync(descriptor)
    except Exception as exc:  # noqa: BLE001
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        if isinstance(exc, LauncherCandidateError):
            raise
        raise LauncherCandidateError("launcher候选写入失败") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    return {
        "schema_id": "lulu.launcher-candidate/v1",
        "candidate_path": str(output),
        "canonical_cli": str(canonical_cli),
        "expected_bundle_id": bundle_id,
        "expected_designated_requirement": requirement,
        "expected_team_id": team_id,
        "candidate_sha256": hashlib.sha256(launcher).hexdigest(),
        "install_authorized": False,
        "authorization_requirement": "REAL_SIGNATURE_AND_INSTALL_REVIEW_REQUIRED",
    }


def generate_ad_hoc_launcher_candidate(
    *,
    app_bundle: Path,
    output_path: Path,
) -> dict[str, object]:
    """Create a source launcher bound to one explicit ad-hoc candidate bundle."""

    bundle = Path(app_bundle)
    output = Path(output_path)
    _validate_owned_path(bundle, directory=True, label="Lulu.app")
    if bundle.name != "Lulu.app":
        raise LauncherCandidateError("Lulu产品包路径无效")
    canonical_cli = bundle / "Contents" / "MacOS" / "lulu-agent-cli"
    cli_metadata = _validate_owned_path(
        canonical_cli,
        directory=False,
        label="Agent CLI",
    )
    if not cli_metadata.st_mode & stat.S_IXUSR:
        raise LauncherCandidateError("Agent CLI缺少owner执行权限")
    if not output.is_absolute() or output.name != "lulu":
        raise LauncherCandidateError("launcher候选必须是显式绝对lulu路径")
    _validate_owned_path(output.parent, directory=True, label="launcher候选目录")
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LauncherCandidateError("launcher候选路径不可用") from exc
    else:
        raise LauncherCandidateError("launcher候选路径已存在，禁止覆盖")

    launcher = (
        "#!/bin/sh\n"
        "# lulu.phase10-ad-hoc-launcher-source/v1\n"
        "# install_authorized=false\n"
        "# signature_kind=ad_hoc\n"
        "# team_id=none\n"
        "# protected_capabilities=unavailable\n"
        "set -eu\n"
        f"exec {shlex.quote(str(canonical_cli))} \"$@\"\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700,
        )
        created = True
        os.fchmod(descriptor, 0o700)
        offset = 0
        while offset < len(launcher):
            written = os.write(descriptor, launcher[offset:])
            if written <= 0:
                raise OSError("launcher候选短写")
            offset += written
        os.fsync(descriptor)
    except Exception as exc:  # noqa: BLE001
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        raise LauncherCandidateError("ad-hoc launcher候选写入失败") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {
        "schema_id": "lulu.phase10-ad-hoc-launcher-source/v1",
        "candidate_path": str(output),
        "candidate_app": str(bundle),
        "canonical_candidate_cli": str(canonical_cli),
        "candidate_sha256": hashlib.sha256(launcher).hexdigest(),
        "signature_kind": "ad_hoc",
        "team_id": None,
        "install_authorized": False,
        "protected_capabilities": "unavailable",
    }
