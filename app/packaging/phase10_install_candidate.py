#!/usr/bin/env python3
"""Owner-safe, journaled install and rollback for a finalized Lulu candidate."""

from __future__ import annotations

import grp
import hashlib
import json
import os
from pathlib import Path
import plistlib
import runpy
import shlex
import shutil
import stat
import unicodedata
from typing import Iterable

from packaging.phase10_post_build_authority import (
    _fixed_candidate_bundle_for_installer,
    load_fixed_post_build_authority,
)
from src.core.cli_runtime.installation_ticket import (
    ValidatedAuthority,
    _protected_formal_app_snapshot,
    validate_authority,
)


_OWNER = "lulu.phase10.install-candidate/v1"
_LAUNCHER_SOURCE_MARKER = "# lulu.phase10-ad-hoc-launcher-source/v1"
_LAUNCHER_TARGET_MARKER = "# lulu.phase10-managed-launcher/v1"
_SKILL_OWNER_FILE = ".lulu-phase10-owner.json"
_SYSTEM_APPLICATIONS_ROOT = Path("/Applications")
_SYSTEM_APPLICATION_TARGET = _SYSTEM_APPLICATIONS_ROOT / "Lulu.app"
_TRUSTED_TEST_APPLICATION_TARGET = (
    _SYSTEM_APPLICATIONS_ROOT / "Lulu Trusted Test.app"
)
_TARGET_IDENTITY = {
    "bundle_id": "com.lulu.transcribe.trusted-test",
    "channel": "trusted-test",
    "product_name": "Lulu Trusted Test",
    "profile": "trusted_test",
}
_EXPECTED_SKILL_FILES = frozenset(
    {
        "SKILL.md",
        "manifest.json",
        "scripts/verify_lulu_install.py",
        "references/protocol.md",
        "references/workflows.md",
        "references/errors.md",
        "references/trust-anchor.md",
    }
)


class CandidateInstallError(RuntimeError):
    """Candidate installation or rollback failed closed."""


def _absolute(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise CandidateInstallError(f"{label}必须显式为绝对路径")
    return value


def _no_symlink_ancestry(path: Path, label: str) -> None:
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CandidateInstallError(f"{label}祖先禁止符号链接")
    except CandidateInstallError:
        raise
    except OSError as exc:
        raise CandidateInstallError(f"{label}不可用") from exc


def _owned_existing(
    path: Path,
    *,
    directory: bool,
    label: str,
    executable: bool = False,
) -> os.stat_result:
    value = _absolute(path, label)
    _no_symlink_ancestry(value, label)
    try:
        metadata = value.lstat()
    except OSError as exc:
        raise CandidateInstallError(f"{label}不可用") from exc
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
        or (executable and not metadata.st_mode & stat.S_IXUSR)
    ):
        raise CandidateInstallError(f"{label}owner、mode或类型无效")
    return metadata


def _target_parent(path: Path, label: str) -> Path:
    value = _absolute(path, label)
    if _SYSTEM_APPLICATIONS_ROOT in value.parents:
        if value != _TRUSTED_TEST_APPLICATION_TARGET or ".." in value.parts:
            raise CandidateInstallError(
                f"{label}系统安装路径必须精确为"
                "/Applications/Lulu Trusted Test.app"
            )
        executing_uid = os.getuid()
        if executing_uid == 0:
            raise CandidateInstallError(f"{label}禁止通过root/sudo绕过归属校验")
        _no_symlink_ancestry(_SYSTEM_APPLICATIONS_ROOT, f"{label}父目录")
        try:
            parent_metadata = _SYSTEM_APPLICATIONS_ROOT.lstat()
            admin_gid = int(grp.getgrnam("admin").gr_gid)
            executing_groups = set(os.getgroups()) | {os.getgid()}
        except OSError as exc:
            raise CandidateInstallError(f"{label}系统安装根不可用") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateInstallError(f"{label}无法冻结admin组") from exc
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_gid != admin_gid
            or parent_metadata.st_mode & 0o002
            or admin_gid not in executing_groups
        ):
            raise CandidateInstallError(f"{label}系统安装根owner、group或mode无效")
        try:
            target_metadata = value.lstat()
        except FileNotFoundError:
            return value
        except OSError as exc:
            raise CandidateInstallError(f"{label}不可用") from exc
        if (
            stat.S_ISLNK(target_metadata.st_mode)
            or not stat.S_ISDIR(target_metadata.st_mode)
            or target_metadata.st_uid != executing_uid
            or target_metadata.st_mode & 0o022
        ):
            raise CandidateInstallError(f"{label}现有App归属、mode或类型无效")
        return value
    _owned_existing(value.parent, directory=True, label=f"{label}父目录")
    try:
        metadata = value.lstat()
    except FileNotFoundError:
        return value
    except OSError as exc:
        raise CandidateInstallError(f"{label}不可用") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CandidateInstallError(f"{label}禁止符号链接")
    return value


def _read_regular(path: Path, label: str) -> bytes:
    _owned_existing(path, directory=False, label=label)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CandidateInstallError(f"{label}读取失败") from exc


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(_read_regular(path, label).decode("utf-8"))
    except CandidateInstallError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CandidateInstallError(f"{label}无法解析") from exc
    if type(payload) is not dict:
        raise CandidateInstallError(f"{label}必须是JSON对象")
    return payload


def _iter_tree(root: Path) -> Iterable[tuple[Path, str, os.stat_result]]:
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            entries = sorted(os.scandir(parent), key=lambda item: item.name)
        except OSError as exc:
            raise CandidateInstallError("目录无法安全枚举") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise CandidateInstallError("目录条目无法读取") from exc
            relative = path.relative_to(root).as_posix()
            yield path, relative, metadata
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)


def _path_manifest(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError as exc:
        raise CandidateInstallError("目标身份无法读取") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CandidateInstallError("目标禁止符号链接")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise CandidateInstallError("目标包含非owner或可被他人写入的内容")
    if stat.S_ISREG(metadata.st_mode):
        raw = path.read_bytes()
        return {
            "kind": "file",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    if not stat.S_ISDIR(metadata.st_mode):
        raise CandidateInstallError("目标类型不受支持")
    records: list[bytes] = []
    total_bytes = 0
    count = 0
    for child, relative, child_metadata in _iter_tree(path):
        if child_metadata.st_uid != os.getuid() or child_metadata.st_mode & 0o022:
            raise CandidateInstallError("目录包含非owner或可被他人写入的内容")
        mode = stat.S_IMODE(child_metadata.st_mode)
        if stat.S_ISDIR(child_metadata.st_mode):
            records.append(f"D\0{relative}\0{mode:o}\n".encode("utf-8"))
        elif stat.S_ISLNK(child_metadata.st_mode):
            target = os.readlink(child)
            if Path(target).is_absolute():
                raise CandidateInstallError("目录包含绝对符号链接")
            records.append(
                f"L\0{relative}\0{mode:o}\0{target}\n".encode("utf-8")
            )
        elif stat.S_ISREG(child_metadata.st_mode):
            raw = child.read_bytes()
            count += 1
            total_bytes += len(raw)
            records.append(
                f"F\0{relative}\0{mode:o}\0{len(raw)}\0"
                f"{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8")
            )
        else:
            raise CandidateInstallError("目录包含不支持的文件类型")
    return {
        "kind": "directory",
        "tree_sha256": hashlib.sha256(b"".join(sorted(records))).hexdigest(),
        "regular_file_count": count,
        "regular_bytes": total_bytes,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _copy_tree_no_follow(source: Path, destination: Path) -> None:
    try:
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            copy_function=lambda src, dst: shutil.copy2(
                src,
                dst,
                follow_symlinks=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise CandidateInstallError("候选目录暂存失败") from exc


def _write_exclusive(path: Path, raw: bytes, mode: int) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        created = True
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("短写")
            offset += written
        os.fsync(descriptor)
    except Exception as exc:  # noqa: BLE001
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise CandidateInstallError("受控文件写入失败") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, payload: dict[str, object]) -> None:
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.phase10-write"
    try:
        temporary.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CandidateInstallError("journal临时路径不可用") from exc
    else:
        raise CandidateInstallError("journal临时路径已存在")
    _write_exclusive(temporary, raw, 0o600)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception as exc:  # noqa: BLE001
        raise CandidateInstallError("journal原子提交失败") from exc


def _validate_candidate(
    *,
    candidate_bundle: Path,
    candidate_evidence: Path,
    codesign_runner: object | None,
) -> dict[str, object]:
    bundle = _absolute(candidate_bundle, "候选App")
    _owned_existing(bundle, directory=True, label="候选App")
    evidence = _read_json(candidate_evidence, "候选证据")
    finalizer = runpy.run_path(
        str(Path(__file__).resolve().with_name("phase10_finalize_candidate.py"))
    )
    try:
        actual = finalizer["inspect_candidate_bundle"](bundle)
        if codesign_runner is None:
            actual_signature = finalizer["inspect_ad_hoc_signature"](bundle)
        else:
            actual_signature = finalizer["inspect_ad_hoc_signature"](
                bundle,
                codesign_runner,
            )
    except Exception as exc:  # noqa: BLE001
        raise CandidateInstallError("候选App无法重新核验") from exc
    expected_signature = {
        "kind": "ad_hoc",
        "codesign_verified": True,
        "team_id": None,
        "protected_capabilities": "unavailable",
    }
    trusted_test = bundle.name == "Lulu-Trusted-Test.app"
    expected_identity = {
        "version": "0.3.2",
        "build": "20260823.1",
        "channel": "trusted-test" if trusted_test else "candidate",
        "release_state": (
            "trusted_test_non_release" if trusted_test else "non_release"
        ),
    }
    if (
        evidence.get("schema_id") != "lulu.phase10-candidate-evidence/v1"
        or evidence.get("schema_revision") != 1
        or evidence.get("evidence_scope") != "candidate_only_non_release"
        or evidence.get("bundle_role") != "dist"
        or evidence.get("bundle_path") != str(bundle)
        or evidence.get("candidate_identity")
        != expected_identity
        or actual_signature != expected_signature
        or evidence.get("signature") != actual_signature
        or any(evidence.get(field) != value for field, value in actual.items())
    ):
        raise CandidateInstallError("候选证据与当前App字节身份不一致")
    return evidence


def _validate_launcher_source(path: Path, candidate_bundle: Path) -> None:
    raw = _read_regular(path, "launcher source")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateInstallError("launcher source编码无效") from exc
    candidate_cli = candidate_bundle / "Contents" / "MacOS" / "lulu-agent-cli"
    expected_exec = f"exec {shlex.quote(str(candidate_cli))} \"$@\""
    if (
        not content.startswith("#!/bin/sh\n")
        or _LAUNCHER_SOURCE_MARKER not in content
        or "# signature_kind=ad_hoc" not in content
        or "# team_id=none" not in content
        or "# install_authorized=false" not in content
        or expected_exec not in content
    ):
        raise CandidateInstallError("launcher source不属于当前ad-hoc候选")


def _validate_skill_source(path: Path, candidate_bundle: Path) -> None:
    expected = candidate_bundle / "Contents" / "Resources" / "skills" / "lulu-agent"
    if path != expected:
        raise CandidateInstallError("Skill source必须来自当前候选App")
    _owned_existing(path, directory=True, label="Skill source")
    if any(item.is_symlink() for item in path.rglob("*")):
        raise CandidateInstallError("Skill source禁止符号链接")
    actual_files = {
        str(item.relative_to(path))
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    }
    if actual_files != _EXPECTED_SKILL_FILES:
        raise CandidateInstallError("Skill source文件集合不一致")
    manifest = _read_json(path / "manifest.json", "Skill manifest")
    proposal = manifest.get("proposal")
    installation = manifest.get("installation")
    if (
        type(proposal) is not dict
        or proposal.get("version") != "lulu.intent-proposal/v1"
        or proposal.get("trust_level") != "untrusted_data_plane"
        or proposal.get("dispatchable") is not False
        or type(installation) is not dict
        or installation.get("automatic") is not False
        or _path_manifest(path).get("regular_bytes", 1_000_001) >= 1_000_000
    ):
        raise CandidateInstallError("Skill必须小于1MB且保持proposal-only")


def _validate_existing_targets(
    *,
    app_target: Path,
    launcher_target: Path,
    skill_target: Path,
) -> None:
    if app_target.exists():
        _owned_existing(app_target, directory=True, label="现有Trusted Test App")
        try:
            info = plistlib.loads(
                _read_regular(
                    app_target / "Contents" / "Info.plist",
                    "现有Trusted Test App Info.plist",
                )
            )
        except CandidateInstallError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CandidateInstallError("现有Trusted Test App身份无法解析") from exc
        if (
            type(info) is not dict
            or info.get("CFBundleIdentifier")
            != "com.lulu.transcribe.trusted-test"
        ):
            raise CandidateInstallError("拒绝覆盖非Trusted Test产品App")
    if launcher_target.exists():
        content = _read_regular(launcher_target, "现有launcher").decode(
            "utf-8",
            errors="strict",
        )
        if (
            _LAUNCHER_TARGET_MARKER not in content
            or f"# owner_tool={_OWNER}" not in content
        ):
            raise CandidateInstallError("拒绝覆盖非本工具归属的launcher")
    if skill_target.exists():
        _owned_existing(skill_target, directory=True, label="现有Skill")
        owner = _read_json(skill_target / _SKILL_OWNER_FILE, "现有Skill归属")
        if owner != {"owner": _OWNER}:
            raise CandidateInstallError("拒绝覆盖非本工具归属的Skill")


def _authority_paths(authority: ValidatedAuthority) -> dict[str, Path]:
    if (
        authority.target_role != "trusted_test"
        or authority.target_identity != _TARGET_IDENTITY
        or any(
            authority.candidate.get(field) != value
            for field, value in _TARGET_IDENTITY.items()
        )
        or authority.candidate.get("target_role") != authority.target_role
        or authority.candidate.get("installation_target")
        != authority.installation_target
    ):
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    app = Path(authority.installation_target)
    if (
        not app.is_absolute()
        or app.name != "Lulu Trusted Test.app"
        or unicodedata.normalize("NFC", str(app)) != str(app)
    ):
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    plan = Path(authority.plan_path)
    suffix = (
        Path("Lulu/installation-trust/agent-cli")
        / authority.candidate_binding_sha256
        / "ticket-plan.json"
    )
    try:
        support = plan.parents[len(suffix.parts) - 1]
    except IndexError as exc:
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED") from exc
    if (
        plan != support / suffix
        or support.name != "Application Support"
        or support.parent.name != "Library"
    ):
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    home = support.parent.parent
    binding = authority.target_binding_sha256
    launcher = home / ".local/bin/lulu"
    skill = home / ".agents/skills/lulu-agent"
    return {
        "app_target": app,
        "launcher_target": launcher,
        "skill_target": skill,
        "formal_app": app.parent / "Lulu.app",
        "journal_path": plan.with_name("install-transaction.json"),
        "app_stage": app.parent / f".Lulu Trusted Test.app.{binding}.stage",
        "app_backup": app.parent / f".Lulu Trusted Test.app.{binding}.backup",
        "launcher_stage": launcher.parent / f".lulu.{binding}.stage",
        "launcher_backup": launcher.parent / f".lulu.{binding}.backup",
        "skill_stage": skill.parent / f".lulu-agent.{binding}.stage",
        "skill_backup": skill.parent / f".lulu-agent.{binding}.backup",
    }


def _same_object(left: Path, right: Path) -> bool:
    try:
        left_stat = left.lstat()
        right_stat = right.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED") from exc
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev,
        right_stat.st_ino,
    )


def _preflight(
    *,
    authority: ValidatedAuthority,
    candidate_bundle: Path,
    candidate_evidence: Path,
    skill_source: Path,
    codesign_runner: object | None,
) -> dict[str, object]:
    derived = _authority_paths(authority)
    bundle = _absolute(candidate_bundle, "候选App")
    evidence_path = _absolute(candidate_evidence, "候选证据")
    skill = _absolute(skill_source, "Skill source")
    app = _target_parent(derived["app_target"], "Applications target")
    launcher_destination = _target_parent(
        derived["launcher_target"], "launcher target"
    )
    skill_destination = _target_parent(derived["skill_target"], "Skill target")
    journal = _absolute(derived["journal_path"], "journal")
    if (
        app.name != "Lulu Trusted Test.app"
        or launcher_destination.name != "lulu"
        or skill_destination.name != "lulu-agent"
    ):
        raise CandidateInstallError("安装目标名称不符合Lulu产品合同")
    if _same_object(app, derived["formal_app"]):
        raise CandidateInstallError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    try:
        formal_snapshot = _protected_formal_app_snapshot(derived["formal_app"])
    except Exception as exc:  # noqa: BLE001 - read-only protected boundary
        raise CandidateInstallError("FORMAL_APP_PROTECTION_DRIFT") from exc
    if formal_snapshot != authority.formal_app_snapshot:
        raise CandidateInstallError("FORMAL_APP_PROTECTION_DRIFT")
    transaction_paths = (
        journal,
        derived["app_stage"],
        derived["app_backup"],
        derived["launcher_stage"],
        derived["launcher_backup"],
        derived["skill_stage"],
        derived["skill_backup"],
    )
    for target in transaction_paths:
        if target.exists() or target.is_symlink():
            raise CandidateInstallError("INSTALLATION_TRANSACTION_CONFLICT")
    for parent in (
        journal.parent,
        app.parent,
        launcher_destination.parent,
        skill_destination.parent,
    ):
        _owned_existing(parent, directory=True, label="transaction parent")

    evidence = _validate_candidate(
        candidate_bundle=bundle,
        candidate_evidence=evidence_path,
        codesign_runner=codesign_runner,
    )
    source_identity = authority.candidate.get("source_identity")
    if (
        evidence.get("bundle_tree_sha256")
        != authority.candidate.get("app_content_tree_sha256")
        or evidence.get("canonical_cli_sha256")
        != authority.candidate.get("canonical_cli_sha256")
        or type(source_identity) is not dict
        or evidence.get("source_commit")
        != source_identity.get("actual_build_source_commit")
        or evidence.get("source_tree")
        != source_identity.get("actual_build_source_tree")
    ):
        raise CandidateInstallError("CANDIDATE_IDENTITY_MISMATCH")
    _validate_skill_source(skill, bundle)
    _validate_existing_targets(
        app_target=app,
        launcher_target=launcher_destination,
        skill_target=skill_destination,
    )
    return {
        "candidate_bundle": bundle,
        "candidate_evidence": evidence_path,
        "skill_source": skill,
        "app_target": app,
        "launcher_target": launcher_destination,
        "skill_target": skill_destination,
        "journal_path": journal,
        "formal_app": derived["formal_app"],
        "formal_snapshot": formal_snapshot,
        "app_stage": derived["app_stage"],
        "app_backup": derived["app_backup"],
        "launcher_stage": derived["launcher_stage"],
        "launcher_backup": derived["launcher_backup"],
        "skill_stage": derived["skill_stage"],
        "skill_backup": derived["skill_backup"],
        "authority": authority,
        "evidence": evidence,
    }


def _prepare_payload(paths: dict[str, object]) -> dict[str, object]:
    authority = paths["authority"]
    assert isinstance(authority, ValidatedAuthority)
    targets = []
    for key, target, stage, backup in (
        ("app", paths["app_target"], paths["app_stage"], paths["app_backup"]),
        (
            "launcher",
            paths["launcher_target"],
            paths["launcher_stage"],
            paths["launcher_backup"],
        ),
        (
            "skill",
            paths["skill_target"],
            paths["skill_stage"],
            paths["skill_backup"],
        ),
    ):
        original = _path_manifest(target)
        targets.append(
            {
                "key": key,
                "target_path": str(target),
                "stage_path": str(stage),
                "backup_path": str(backup),
                "removed_path": str(
                    target.parent
                    / f".{target.name}.{authority.target_binding_sha256}.removed"
                ),
                "unused_stage_path": str(
                    target.parent
                    / f".{target.name}.{authority.target_binding_sha256}.unused-stage"
                ),
                "original_existed": original["kind"] != "absent",
                "original_manifest": original,
                "installed_manifest": None,
                "backup_moved": False,
                "candidate_installed": False,
            }
        )
    payload = {
        "schema_id": "lulu.phase10-install-journal/v1",
        "schema_revision": 1,
        "owner_tool": _OWNER,
        "state": "prepared",
        "journal_path": str(paths["journal_path"]),
        "candidate_bundle": str(paths["candidate_bundle"]),
        "candidate_evidence": str(paths["candidate_evidence"]),
        "candidate_bundle_tree_sha256": paths["evidence"]["bundle_tree_sha256"],
        "protected_capabilities": "unavailable",
        "candidate_binding_sha256": authority.candidate_binding_sha256,
        "installation_id": authority.installation_id,
        "installation_target": authority.installation_target,
        "target_identity": authority.target_identity,
        "target_role": authority.target_role,
        "target_binding_sha256": authority.target_binding_sha256,
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "formal_app_path": str(paths["formal_app"]),
        "formal_app_snapshot": paths["formal_snapshot"],
        "shared_user_data_touched": False,
        "all_originals_backed_up": False,
        "targets": targets,
    }
    return payload


def _stage_payload(
    paths: dict[str, object],
    payload: dict[str, object],
    *,
    journal_path: Path,
    fault_after: str | None,
) -> None:
    by_key = {target["key"]: target for target in payload["targets"]}

    app = by_key["app"]
    app_stage = Path(app["stage_path"])
    _copy_tree_no_follow(paths["candidate_bundle"], app_stage)
    app["installed_manifest"] = _path_manifest(app_stage)
    _write_journal(journal_path, payload)
    if fault_after == "stage_app":
        raise CandidateInstallError("故障注入:stage_app")

    skill = by_key["skill"]
    skill_stage = Path(skill["stage_path"])
    _copy_tree_no_follow(paths["skill_source"], skill_stage)
    _write_exclusive(
        skill_stage / _SKILL_OWNER_FILE,
        (json.dumps({"owner": _OWNER}, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
        0o600,
    )
    skill["installed_manifest"] = _path_manifest(skill_stage)
    _write_journal(journal_path, payload)
    if fault_after == "stage_skill":
        raise CandidateInstallError("故障注入:stage_skill")

    launcher = by_key["launcher"]
    launcher_stage = Path(launcher["stage_path"])
    canonical_cli = paths["app_target"] / "Contents" / "MacOS" / "lulu-agent-cli"
    launcher_raw = (
        "#!/bin/sh\n"
        f"{_LAUNCHER_TARGET_MARKER}\n"
        f"# owner_tool={_OWNER}\n"
        "# signature_kind=ad_hoc\n"
        "# protected_capabilities=unavailable\n"
        "set -eu\n"
        f"exec {shlex.quote(str(canonical_cli))} \"$@\"\n"
    ).encode("utf-8")
    _write_exclusive(launcher_stage, launcher_raw, 0o700)
    launcher["installed_manifest"] = _path_manifest(launcher_stage)
    _write_journal(journal_path, payload)
    if fault_after == "stage_launcher":
        raise CandidateInstallError("故障注入:stage_launcher")
    payload["state"] = "prepared"
    _write_journal(journal_path, payload)


def _move_unused_stages(payload: dict[str, object]) -> None:
    for target in payload["targets"]:
        stage = Path(target["stage_path"])
        unused = Path(target["unused_stage_path"])
        if stage.exists() or stage.is_symlink():
            if unused.exists() or unused.is_symlink():
                raise CandidateInstallError("未使用暂存归档路径已存在")
            os.replace(stage, unused)


def _restore_payload(
    payload: dict[str, object],
    *,
    journal_path: Path,
    terminal_state: str,
) -> dict[str, object]:
    for target in reversed(payload["targets"]):
        destination = Path(target["target_path"])
        backup = Path(target["backup_path"])
        removed = Path(target["removed_path"])
        current = _path_manifest(destination)
        if target["candidate_installed"] or (
            target["backup_moved"] and current != {"kind": "absent"}
        ):
            if current != target["installed_manifest"]:
                raise CandidateInstallError("回滚目标已漂移，拒绝覆盖")
            if removed.exists() or removed.is_symlink():
                raise CandidateInstallError("回滚候选归档路径已存在")
            os.replace(destination, removed)
            target["candidate_installed"] = False
        if target["original_existed"] and target["backup_moved"]:
            if _path_manifest(backup) != target["original_manifest"]:
                raise CandidateInstallError("原始备份字节身份不一致")
            os.replace(backup, destination)
            target["backup_moved"] = False
        if _path_manifest(destination) != target["original_manifest"]:
            raise CandidateInstallError("回滚后原始字节身份未恢复")
    _move_unused_stages(payload)
    formal = Path(str(payload["formal_app_path"]))
    if _protected_formal_app_snapshot(formal) != payload["formal_app_snapshot"]:
        raise CandidateInstallError("FORMAL_APP_PROTECTION_DRIFT")
    payload["state"] = terminal_state
    _write_journal(journal_path, payload)
    return {
        "schema_id": "lulu.phase10-install-result/v1",
        "state": terminal_state,
        "journal_path": str(journal_path),
        "installation_target": payload["installation_target"],
        "target_role": payload["target_role"],
        "target_binding_sha256": payload["target_binding_sha256"],
        "formal_app_snapshot": payload["formal_app_snapshot"],
        "shared_user_data_touched": False,
    }


def _install_candidate_transaction(
    *,
    locator_receipt: object,
    plan_receipt: object,
    candidate_bundle: Path,
    candidate_evidence: Path,
    fault_after: str | None = None,
    codesign_runner: object | None = None,
) -> dict[str, object]:
    """Private Fake-capable transaction core used by the public fixed entry."""

    if fault_after not in {
        None,
        "stage_app",
        "stage_skill",
        "stage_launcher",
        "app",
        "launcher",
        "skill",
    }:
        raise CandidateInstallError("未知故障注入点")
    try:
        authority = validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - exact private receipt boundary
        reason = getattr(exc, "reason_code", "INSTALLATION_TARGET_AUTHORITY_REQUIRED")
        raise CandidateInstallError(str(reason)) from exc
    paths = _preflight(
        authority=authority,
        candidate_bundle=candidate_bundle,
        candidate_evidence=candidate_evidence,
        skill_source=(
            candidate_bundle / "Contents/Resources/skills/lulu-agent"
        ),
        codesign_runner=codesign_runner,
    )
    journal = paths["journal_path"]
    assert isinstance(journal, Path)
    payload: dict[str, object] | None = None
    try:
        payload = _prepare_payload(paths)
        _write_journal(journal, payload)
        _stage_payload(
            paths,
            payload,
            journal_path=journal,
            fault_after=fault_after,
        )
        for target in payload["targets"]:
            destination = Path(target["target_path"])
            backup = Path(target["backup_path"])
            if target["original_existed"]:
                os.replace(destination, backup)
                target["backup_moved"] = True
                _write_journal(journal, payload)
        payload["all_originals_backed_up"] = True
        _write_journal(journal, payload)
        for target in payload["targets"]:
            destination = Path(target["target_path"])
            stage = Path(target["stage_path"])
            os.replace(stage, destination)
            target["candidate_installed"] = True
            _write_journal(journal, payload)
            if fault_after == target["key"]:
                raise CandidateInstallError(f"故障注入:{fault_after}")
        payload["state"] = "installed"
        if (
            _protected_formal_app_snapshot(paths["formal_app"])
            != paths["formal_snapshot"]
        ):
            raise CandidateInstallError("FORMAL_APP_PROTECTION_DRIFT")
        _write_journal(journal, payload)
    except Exception as exc:  # noqa: BLE001
        if payload is not None:
            try:
                _restore_payload(
                    payload,
                    journal_path=journal,
                    terminal_state="failed_restored",
                )
            except Exception as restore_exc:  # noqa: BLE001
                raise CandidateInstallError("安装失败且自动恢复未能完成") from restore_exc
        if isinstance(exc, CandidateInstallError):
            raise
        raise CandidateInstallError("安装事务失败并已自动恢复") from exc
    return {
        "schema_id": "lulu.phase10-install-result/v1",
        "state": "installed",
        "journal_path": str(journal),
        "effect_scope": "explicit_local_install_targets",
        "candidate_binding_sha256": authority.candidate_binding_sha256,
        "installation_id": authority.installation_id,
        "installation_target": authority.installation_target,
        "target_identity": authority.target_identity,
        "target_role": authority.target_role,
        "target_binding_sha256": authority.target_binding_sha256,
        "locator_sha256": authority.locator_sha256,
        "plan_sha256": authority.plan_sha256,
        "formal_app_snapshot": paths["formal_snapshot"],
        "shared_user_data_touched": False,
        "protected_capabilities": "unavailable",
    }


def install_candidate(
    *,
    locator_receipt: object,
    plan_receipt: object,
) -> dict[str, object]:
    """Install the one fixed candidate into the exact receipt-bound namespace."""

    try:
        post_build_receipt = load_fixed_post_build_authority()
        validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
            post_build_receipt=post_build_receipt,
        )
        candidate = _fixed_candidate_bundle_for_installer(post_build_receipt)
    except Exception as exc:  # noqa: BLE001 - fixed post-build authority boundary
        reason = getattr(exc, "reason_code", "POST_BUILD_AUTHORITY_REQUIRED")
        raise CandidateInstallError(str(reason)) from exc
    return _install_candidate_transaction(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
        candidate_bundle=candidate,
        candidate_evidence=candidate.parent / "candidate-evidence.json",
    )


def rollback_install(
    *,
    locator_receipt: object,
    plan_receipt: object,
) -> dict[str, object]:
    """Restore exact original App/launcher/Skill identities from one owned journal."""

    try:
        authority = validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - exact private receipt boundary
        reason = getattr(exc, "reason_code", "INSTALLATION_TARGET_AUTHORITY_REQUIRED")
        raise CandidateInstallError(str(reason)) from exc
    expected_paths = _authority_paths(authority)
    journal = _absolute(expected_paths["journal_path"], "journal")
    payload = _read_json(journal, "安装journal")
    if (
        payload.get("schema_id") != "lulu.phase10-install-journal/v1"
        or payload.get("schema_revision") != 1
        or payload.get("owner_tool") != _OWNER
        or payload.get("journal_path") != str(journal)
        or payload.get("state") != "installed"
        or payload.get("all_originals_backed_up") is not True
        or payload.get("candidate_binding_sha256")
        != authority.candidate_binding_sha256
        or payload.get("installation_id") != authority.installation_id
        or payload.get("installation_target") != authority.installation_target
        or payload.get("target_identity") != authority.target_identity
        or payload.get("target_role") != authority.target_role
        or payload.get("target_binding_sha256")
        != authority.target_binding_sha256
        or payload.get("locator_sha256") != authority.locator_sha256
        or payload.get("plan_sha256") != authority.plan_sha256
        or type(payload.get("targets")) is not list
        or len(payload["targets"]) != 3
    ):
        raise CandidateInstallError("安装journal状态或归属无效")
    if payload.get("shared_user_data_touched") is not False:
        raise CandidateInstallError("安装journal数据边界无效")
    expected_names = {
        "app": "Lulu Trusted Test.app",
        "launcher": "lulu",
        "skill": "lulu-agent",
    }
    expected_targets = {
        "app": expected_paths["app_target"],
        "launcher": expected_paths["launcher_target"],
        "skill": expected_paths["skill_target"],
    }
    expected_stages = {
        "app": expected_paths["app_stage"],
        "launcher": expected_paths["launcher_stage"],
        "skill": expected_paths["skill_stage"],
    }
    expected_backups = {
        "app": expected_paths["app_backup"],
        "launcher": expected_paths["launcher_backup"],
        "skill": expected_paths["skill_backup"],
    }
    seen: set[str] = set()
    for target in payload["targets"]:
        if type(target) is not dict:
            raise CandidateInstallError("安装journal目标格式无效")
        key = target.get("key")
        if key not in expected_names or key in seen:
            raise CandidateInstallError("安装journal目标集合无效")
        seen.add(str(key))
        destination = _absolute(
            Path(str(target.get("target_path", ""))),
            f"journal {key} target",
        )
        _target_parent(destination, f"journal {key} target")
        if (
            destination.name != expected_names[str(key)]
            or destination != expected_targets[str(key)]
        ):
            raise CandidateInstallError("安装journal目标名称无效")
        if Path(str(target.get("stage_path", ""))) != expected_stages[str(key)]:
            raise CandidateInstallError("安装journal目标与暂存路径绑定不一致")
        if Path(str(target.get("backup_path", ""))) != expected_backups[str(key)]:
            raise CandidateInstallError("安装journal备份路径绑定不一致")
        if type(target.get("original_manifest")) is not dict or type(
            target.get("installed_manifest")
        ) is not dict:
            raise CandidateInstallError("安装journal字节清单无效")
    if seen != set(expected_names):
        raise CandidateInstallError("安装journal缺少产品目标")
    return _restore_payload(
        payload,
        journal_path=journal,
        terminal_state="rolled_back",
    )
