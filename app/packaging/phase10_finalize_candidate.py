#!/usr/bin/env python3
"""Validate and seal evidence for one explicit Phase 10 Lulu candidate bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
from typing import Callable


_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from src.core.agent_entry.canonical_app_tree import (
    CanonicalAppTreeError,
    canonical_app_tree_identity,
)


_VERSION = "0.3.2"
_BUILD = "20260823.4"
_TRUSTED_PACKAGE_SURFACE = "gui_only/v1"
_DIAGNOSTIC_IDENTITY = {
    "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
    "diagnostic_only": True,
    "promotion_allowed": False,
    "retained_install_allowed": False,
}
_GUI_ONLY_EVIDENCE_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "evidence_scope",
        "package_surface",
        "bundle_role",
        "bundle_path",
        "candidate_identity",
        "gui_entry",
        "signature",
        "forbidden_surface_absent",
        "bundle_tree_sha256",
        "bundle_file_count",
        "bundle_regular_bytes",
        "gui_executable_sha256",
        "build_profile_sha256",
        "source_commit",
        "source_tree",
        *_DIAGNOSTIC_IDENTITY,
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_SHARED_RUNTIME = {
    "analysis": "single",
    "python": "single",
    "core": "single",
    "resources": "single",
    "storage_layout": "lulu.shared-storage/v1",
}
_FORBIDDEN_DUPLICATE_PARTS = frozenset(
    {".venv", "venv", "agent-runtime", "agent-data", "agent_data"}
)
_BUNDLE_IDENTITIES = {
    "Lulu.app": {
        "app_name": "Lulu",
        "artifact_prefix": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "channel": "candidate",
        "entry": "run_external.py",
        "gui": "Lulu",
        "profile": "external_candidate",
        "release_state": "non_release",
    },
    "Lulu-Trusted-Test.app": {
        "app_name": "Lulu Trusted Test",
        "artifact_prefix": "Lulu-Trusted-Test",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "channel": "trusted-test",
        "entry": "run_trusted_test.py",
        "gui": "Lulu-Trusted-Test",
        "profile": "trusted_test",
        "release_state": "trusted_test_non_release",
        "package_surface": _TRUSTED_PACKAGE_SURFACE,
    },
}
CodesignRunner = Callable[[list[str]], object]


class CandidateFinalizeError(RuntimeError):
    """The supplied bundle cannot become Phase 10 candidate evidence."""


def _validate_absolute(path: Path, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise CandidateFinalizeError(f"{label}必须显式为绝对路径")
    return value


def _validate_no_symlink_ancestry(path: Path, label: str) -> None:
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CandidateFinalizeError(f"{label}祖先禁止符号链接")
    except CandidateFinalizeError:
        raise
    except OSError as exc:
        raise CandidateFinalizeError(f"{label}不可用") from exc


def _owned_path(path: Path, *, directory: bool, label: str) -> os.stat_result:
    value = _validate_absolute(path, label)
    _validate_no_symlink_ancestry(value, label)
    try:
        metadata = value.lstat()
    except OSError as exc:
        raise CandidateFinalizeError(f"{label}不可用") from exc
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise CandidateFinalizeError(f"{label}owner、mode或类型无效")
    return metadata


def _read_regular_bytes(path: Path, label: str) -> bytes:
    _owned_path(path, directory=False, label=label)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CandidateFinalizeError(f"{label}读取失败") from exc


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(_read_regular_bytes(path, label).decode("utf-8"))
    except CandidateFinalizeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CandidateFinalizeError(f"{label}无法解析") from exc
    if type(payload) is not dict:
        raise CandidateFinalizeError(f"{label}必须是JSON对象")
    return payload


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_internal_symlink(
    bundle: Path,
    path: Path,
    relative: str,
    target: str,
) -> None:
    target_path = Path(target)
    if target_path.is_absolute():
        raise CandidateFinalizeError("候选App包含绝对符号链接")
    normalized = Path(
        os.path.normpath(str(Path(relative).parent.joinpath(target_path)))
    )
    if normalized.parts and normalized.parts[0] == "..":
        raise CandidateFinalizeError("候选App符号链接可能逃逸产品包")
    try:
        resolved_bundle = bundle.resolve(strict=True)
        resolved_target = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CandidateFinalizeError("候选App符号链接循环或无法解析") from exc
    try:
        resolved_target.relative_to(resolved_bundle)
    except ValueError as exc:
        raise CandidateFinalizeError("候选App符号链接可能逃逸产品包") from exc


def _legacy_tree_digest(bundle: Path) -> tuple[str, int, int]:
    records: list[bytes] = []
    total_bytes = 0
    file_count = 0
    pending = [bundle]
    while pending:
        parent = pending.pop()
        try:
            entries = sorted(os.scandir(parent), key=lambda item: item.name)
        except OSError as exc:
            raise CandidateFinalizeError("候选App目录无法枚举") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(bundle).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise CandidateFinalizeError("候选App条目无法读取") from exc
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise CandidateFinalizeError("候选App包含非owner或可被他人写入的条目")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                records.append(f"D\0{relative}\0{mode:o}\n".encode("utf-8"))
                pending.append(path)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                _validate_internal_symlink(bundle, path, relative, target)
                records.append(
                    f"L\0{relative}\0{mode:o}\0{target}\n".encode("utf-8")
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CandidateFinalizeError("候选App包含不支持的文件类型")
            raw = path.read_bytes()
            file_count += 1
            total_bytes += len(raw)
            records.append(
                f"F\0{relative}\0{mode:o}\0{len(raw)}\0{_sha256(raw)}\n".encode(
                    "utf-8"
                )
            )
    return _sha256(b"".join(sorted(records))), file_count, total_bytes


def _tree_digest(bundle: Path) -> tuple[str, int, int]:
    if (bundle/"Contents/Resources/component_delivery.v2.json").exists():
        from src.core.agent_entry.delivery_identity import delivery_app_tree_identity
        identity=delivery_app_tree_identity(bundle)
        return str(identity["sha256"]),int(identity["regular_file_count"]),int(identity["regular_bytes"])
    manifest = bundle / "Contents/Resources/agent_product_manifest.json"
    if bundle.name != "Lulu.app" or not manifest.is_file() or manifest.is_symlink():
        return _legacy_tree_digest(bundle)
    try:
        identity = canonical_app_tree_identity(bundle.resolve())
    except CanonicalAppTreeError as exc:
        raise CandidateFinalizeError(str(exc)) from exc
    return (
        str(identity["sha256"]),
        int(identity["regular_file_count"]),
        int(identity["regular_bytes"]),
    )


def _validate_executable(path: Path, label: str) -> bytes:
    metadata = _owned_path(path, directory=False, label=label)
    if not metadata.st_mode & stat.S_IXUSR:
        raise CandidateFinalizeError(f"{label}缺少owner执行权限")
    return path.read_bytes()


def _validate_bundle_contract(bundle: Path) -> dict[str, object]:
    _owned_path(bundle, directory=True, label="候选Lulu.app")
    identity = _BUNDLE_IDENTITIES.get(bundle.name)
    if identity is None:
        raise CandidateFinalizeError("候选产品包名称不属于固定构建身份")
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    _owned_path(contents, directory=True, label="Contents")
    _owned_path(macos, directory=True, label="Contents/MacOS")
    _owned_path(resources, directory=True, label="Contents/Resources")

    try:
        info = plistlib.loads(_read_regular_bytes(contents / "Info.plist", "Info.plist"))
    except CandidateFinalizeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CandidateFinalizeError("Info.plist无法解析") from exc
    expected_info = {
        "CFBundleIdentifier": identity["bundle_id"],
        "CFBundleExecutable": identity["gui"],
        "CFBundleShortVersionString": _VERSION,
        "CFBundleVersion": _BUILD,
        "LuluBuildProfile": identity["profile"],
        "LuluBuildIdentity": identity["channel"],
    }
    if type(info) is not dict or any(info.get(key) != value for key, value in expected_info.items()):
        raise CandidateFinalizeError("候选Info.plist身份不一致")
    if identity["profile"] == "trusted_test" and info.get("LSBackgroundOnly") not in (None, False):
        raise CandidateFinalizeError("GUI-only候选禁止LSBackgroundOnly")

    gui = macos / identity["gui"]
    gui_bytes = _validate_executable(gui, "GUI可执行文件")
    cli_bytes: bytes | None = None
    manifest_bytes: bytes | None = None
    if identity["profile"] == "external_candidate":
        cli = macos / "lulu-agent-cli"
        cli_bytes = _validate_executable(cli, "Agent CLI可执行文件")
        manifest_path = resources / "agent_product_manifest.json"
        manifest_bytes = _read_regular_bytes(manifest_path, "Agent产品清单")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CandidateFinalizeError("Agent产品清单无法解析") from exc
        expected_candidate = {
            "version": _VERSION,
            "build": _BUILD,
            "channel": "candidate",
            "release_state": "non_release",
            "source_evidence": "source_unverified",
            "artifact_evidence": "unverified_until_dist_finalize",
        }
        if (
            type(manifest) is not dict
            or manifest.get("product_version") != _VERSION
            or manifest.get("product_build") != _BUILD
            or manifest.get("candidate_identity") != expected_candidate
            or manifest.get("shared_runtime") != _SHARED_RUNTIME
            or manifest.get("installation_identity_scope") != "shared_lulu_installation"
        ):
            raise CandidateFinalizeError("Agent产品清单候选身份不一致")

    build_path = resources / "build_profile.json"
    build_bytes = _read_regular_bytes(build_path, "构建身份")
    try:
        build = json.loads(build_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CandidateFinalizeError("构建身份无法解析") from exc
    if (
        type(build) is not dict
        or build.get("schema") != 1
        or build.get("profile") != identity["profile"]
        or build.get("version") != _VERSION
        or build.get("build") != _BUILD
        or build.get("app_name") != identity["app_name"]
        or build.get("bundle_id") != identity["bundle_id"]
        or build.get("artifact_prefix") != identity["artifact_prefix"]
        or build.get("entry") != identity["entry"]
        or (
            identity["profile"] == "trusted_test"
            and build.get("package_surface") != identity.get("package_surface")
        )
        or not _SOURCE_ID.fullmatch(str(build.get("source_commit", "")))
        or not _SOURCE_ID.fullmatch(str(build.get("source_tree", "")))
        or not _SHA256.fullmatch(str(build.get("product_source_sha256", "")))
    ):
        raise CandidateFinalizeError("构建身份与候选版本不一致")

    forbidden_surface_hits: list[str] = []
    for path in bundle.rglob("*"):
        if _FORBIDDEN_DUPLICATE_PARTS.intersection(part.lower() for part in path.parts):
            raise CandidateFinalizeError("候选App包含第二套Agent runtime或用户数据")
        if identity["profile"] == "trusted_test":
            relative = path.relative_to(bundle).as_posix().lower()
            if any(
                marker in relative
                for marker in (
                    "lulu-agent-cli",
                    "run_agent_cli",
                    "agent_product_manifest.json",
                    "capability_catalog.json",
                    "skills/lulu-agent",
                    "gui_host_runtime_hook",
                    "src/agent_cli",
                    "src/core/cli_runtime",
                )
            ):
                forbidden_surface_hits.append(relative)
    if forbidden_surface_hits:
        raise CandidateFinalizeError("GUI-only候选包含Agent或ticket产品面")

    tree_sha256, file_count, total_bytes = _tree_digest(bundle)
    contract = {
        "bundle_tree_sha256": tree_sha256,
        "bundle_file_count": file_count,
        "bundle_regular_bytes": total_bytes,
        "gui_executable_sha256": _sha256(gui_bytes),
        "build_profile_sha256": _sha256(build_bytes),
        "source_commit": build["source_commit"],
        "source_tree": build["source_tree"],
    }
    if cli_bytes is not None and manifest_bytes is not None:
        contract.update(
            canonical_cli_sha256=_sha256(cli_bytes),
            product_manifest_sha256=_sha256(manifest_bytes),
        )
    return contract


def inspect_candidate_bundle(bundle: Path) -> dict[str, object]:
    """Return the deterministic, signature-independent candidate bundle contract."""

    candidate = _validate_absolute(Path(bundle), "候选App")
    return _validate_bundle_contract(candidate)


def _default_codesign_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def inspect_ad_hoc_signature(
    bundle: Path,
    runner: CodesignRunner = _default_codesign_runner,
) -> dict[str, object]:
    """Recheck that one explicit bundle has only the expected ad-hoc identity."""

    candidate = _validate_absolute(Path(bundle), "候选App")
    _owned_path(candidate, directory=True, label="候选Lulu.app")
    verify_argv = ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)]
    verify = runner(verify_argv)
    if getattr(verify, "returncode", 1) != 0:
        raise CandidateFinalizeError("候选App ad-hoc签名校验失败")
    display_argv = ["/usr/bin/codesign", "--display", "--verbose=4", str(candidate)]
    display = runner(display_argv)
    details = str(getattr(display, "stderr", ""))
    if (
        getattr(display, "returncode", 1) != 0
        or "Signature=adhoc" not in details
        or "Authority=" in details
        or "TeamIdentifier=not set" not in details
    ):
        raise CandidateFinalizeError("候选App必须是无Team ID的ad-hoc签名")
    return {
        "kind": "ad_hoc",
        "codesign_verified": True,
        "team_id": None,
        "protected_capabilities": "unavailable",
    }


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    output = _validate_absolute(path, "候选证据输出")
    _owned_path(output.parent, directory=True, label="候选证据目录")
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CandidateFinalizeError("候选证据输出路径不可用") from exc
    else:
        raise CandidateFinalizeError("候选证据输出已存在，禁止覆盖")
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("候选证据短写")
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
        if isinstance(exc, CandidateFinalizeError):
            raise
        raise CandidateFinalizeError("候选证据写入失败") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def finalize_candidate(
    *,
    bundle: Path,
    bundle_role: str,
    output_path: Path,
    codesign_runner: CodesignRunner = _default_codesign_runner,
) -> dict[str, object]:
    """Validate one explicit staged/dist bundle and emit candidate-only evidence."""

    candidate = _validate_absolute(Path(bundle), "候选App")
    output = _validate_absolute(Path(output_path), "候选证据输出")
    if bundle_role not in {"staged", "dist"}:
        raise CandidateFinalizeError("bundle_role必须显式为staged或dist")
    try:
        output.relative_to(candidate)
    except ValueError:
        pass
    else:
        raise CandidateFinalizeError("候选证据不得写入被封口App内部")
    contract = _validate_bundle_contract(candidate)
    identity = _BUNDLE_IDENTITIES[candidate.name]
    signature = inspect_ad_hoc_signature(candidate, codesign_runner)
    if identity["profile"] == "trusted_test":
        evidence: dict[str, object] = {
            "schema_id": "lulu.phase10-gui-only-candidate-evidence/v1",
            "schema_revision": 1,
            "evidence_scope": "trusted_test_gui_only_non_release",
            "package_surface": _TRUSTED_PACKAGE_SURFACE,
            "bundle_role": bundle_role,
            "bundle_path": str(candidate),
            "candidate_identity": {
                "version": _VERSION,
                "build": _BUILD,
                "channel": identity["channel"],
                "release_state": identity["release_state"],
                "bundle_id": identity["bundle_id"],
                "product": identity["app_name"],
            },
            "gui_entry": f"Contents/MacOS/{identity['gui']}",
            "signature": signature,
            "forbidden_surface_absent": {
                "agent_cli_executable": True,
                "agent_skill": True,
                "agent_manifest_and_catalog": True,
                "agent_runtime_hook": True,
                "agent_python_runtime": True,
                "keychain_ticket_runtime": True,
            },
            **_DIAGNOSTIC_IDENTITY,
            **contract,
        }
        if set(evidence) != _GUI_ONLY_EVIDENCE_FIELDS:
            raise CandidateFinalizeError("GUI-only候选证据字段未封闭")
    else:
        evidence = {
            "schema_id": "lulu.phase10-candidate-evidence/v1",
            "schema_revision": 1,
            "evidence_scope": "candidate_only_non_release",
            "bundle_role": bundle_role,
            "bundle_path": str(candidate),
            "candidate_identity": {
                "version": _VERSION,
                "build": _BUILD,
                "channel": identity["channel"],
                "release_state": identity["release_state"],
            },
            "adapters": {
                "gui": f"Contents/MacOS/{identity['gui']}",
                "agent_cli": "Contents/MacOS/lulu-agent-cli",
            },
            "shared_runtime": dict(_SHARED_RUNTIME),
            "installation_identity_scope": "shared_lulu_installation",
            "signature": signature,
            "protected_capabilities": {
                "state": "unavailable",
                "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            },
            **contract,
        }
    _write_evidence(output, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-role", choices=("staged", "dist"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    finalize_candidate(
        bundle=Path(args.bundle),
        bundle_role=args.bundle_role,
        output_path=Path(args.output),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
