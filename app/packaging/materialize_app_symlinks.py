#!/usr/bin/env python3
"""Materialize every in-bundle symlink before sealing the generation-9 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


SCHEMA_ID = "lulu.agent-symlink-materialization/v1"
BOUND_SCHEMA_ID = "lulu.agent-symlink-materialization/v2"

_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from src.core.agent_entry.canonical_app_tree import canonical_app_tree_identity


_SELF_REFERENTIAL_MANIFEST_ALIAS = (
    "Contents/Frameworks/agent_product_manifest.json"
)
_SELF_REFERENTIAL_MANIFEST_TARGET = (
    "Contents/Resources/agent_product_manifest.json"
)
_SELF_REFERENTIAL_MANIFEST_RAW_TARGET = (
    "../Resources/agent_product_manifest.json"
)
_OMIT_SELF_REFERENTIAL_MANIFEST_ALIAS = (
    "omit_self_referential_manifest_alias"
)


class MaterializationError(RuntimeError):
    pass


def _verify_component_manifest(app: Path, manifest: Path) -> dict[str, object]:
    from src.core.agent_entry.component_manifest import verify_component_manifest

    return verify_component_manifest(app, manifest)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path, reason: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError(reason)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(reason) from exc
    if type(payload) is not dict:
        raise MaterializationError(reason)
    return payload


def _tree_inventory(root: Path) -> tuple[list[str], list[dict[str, object]], str]:
    names: list[str] = []
    records: list[dict[str, object]] = []

    def visit(current: Path) -> None:
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                names.append(relative)
                records.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "mode": mode,
                        "target": os.readlink(path),
                    }
                )
            elif stat.S_ISDIR(metadata.st_mode):
                records.append({"path": relative, "type": "dir", "mode": mode})
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                names.append(relative)
                records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": mode,
                        "size": metadata.st_size,
                        "sha256": _sha256(path),
                    }
                )
            else:
                raise MaterializationError(
                    f"unsupported special file: {relative}"
                )

    visit(root)
    return sorted(names), records, _canonical_digest(records)


def _object_identity(path: Path) -> dict[str, object]:
    root = path
    records: list[dict[str, object]] = []

    def add(current: Path, relative: str) -> None:
        metadata = current.lstat()
        mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
        if stat.S_ISLNK(metadata.st_mode):
            raise MaterializationError("MATERIALIZED_OBJECT_SYMLINK_FORBIDDEN")
        if stat.S_ISREG(metadata.st_mode):
            records.append(
                {
                    "path": relative,
                    "type": "regular",
                    "mode": mode,
                    "size": metadata.st_size,
                    "sha256": _sha256(current),
                }
            )
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise MaterializationError("MATERIALIZED_OBJECT_SPECIAL_FILE")
        records.append({"path": relative, "type": "directory", "mode": mode})
        for child in sorted(current.iterdir(), key=lambda item: item.name.encode("utf-8")):
            child_relative = child.name if not relative else f"{relative}/{child.name}"
            add(child, child_relative)

    add(root, "")
    return {
        "schema_id": "lulu.materialized-object/v1",
        "record_count": len(records),
        "sha256": _canonical_digest(records),
    }


def _default_codesign_runner(app: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_regular(source: Path, destination: Path) -> None:
    metadata = source.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise MaterializationError("target is not a regular file")
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination, follow_symlinks=True)
    os.chmod(destination, stat.S_IMODE(metadata.st_mode), follow_symlinks=False)


def _copy_tree(source: Path, destination: Path, root: Path, active: frozenset[Path]) -> None:
    resolved = source.resolve(strict=True)
    if not _inside(root, resolved):
        raise MaterializationError("symlink target escapes app bundle")
    if resolved in active:
        raise MaterializationError("symlink cycle")
    metadata = resolved.stat()
    if stat.S_ISREG(metadata.st_mode):
        _copy_regular(resolved, destination)
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise MaterializationError("target is not a regular file or directory")
    destination.mkdir(mode=stat.S_IMODE(metadata.st_mode))
    os.chmod(destination, stat.S_IMODE(metadata.st_mode), follow_symlinks=False)
    next_active = active | {resolved}
    for child in sorted(resolved.iterdir(), key=lambda item: item.name):
        _copy_tree(child, destination / child.name, root, next_active)


def materialize(
    app_bundle: Path,
    *,
    signed_profile_receipt: Path | None = None,
    codesign_runner=_default_codesign_runner,
) -> dict[str, object]:
    root = app_bundle.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise MaterializationError("app bundle must be a real directory")

    signed_profile: dict[str, object] | None = None
    pre_materialization: dict[str, object] | None = None
    signed_profile_path: Path | None = None
    if signed_profile_receipt is not None:
        signed_profile_path = Path(signed_profile_receipt).resolve(strict=True)
        if _inside(root, signed_profile_path):
            raise MaterializationError("SIGNED_PROFILE_RECEIPT_INSIDE_APP")
        signed_profile = _read_json(
            signed_profile_path, "SIGNED_STAGE_PROFILE_RECEIPT_INVALID"
        )
        checks = signed_profile.get("checks")
        digests = signed_profile.get("digests")
        app_tree = signed_profile.get("app_tree")
        if (
            signed_profile.get("schema") != 2
            or signed_profile.get("profile") != "external_candidate"
            or signed_profile.get("ok") is not True
            or type(checks) is not dict
            or checks.get("bundle_seal_valid") is not True
            or type(digests) is not dict
            or type(app_tree) is not dict
            or app_tree.get("state") != "pending_materialization"
        ):
            raise MaterializationError("SIGNED_STAGE_PROFILE_RECEIPT_INVALID")
        seal = codesign_runner(root)
        if seal.returncode != 0:
            raise MaterializationError("SIGNED_STAGE_BUNDLE_SEAL_INVALID")
        resources = root / "Contents/Resources"
        _names, _records, resource_digest = _tree_inventory(resources)
        code_resources = root / "Contents/_CodeSignature/CodeResources"
        if (
            resource_digest != digests.get("resources_tree_sha256")
            or not code_resources.is_file()
            or code_resources.is_symlink()
            or _sha256(code_resources) != digests.get("code_resources_sha256")
        ):
            raise MaterializationError("SIGNED_STAGE_PROFILE_BINDING_MISMATCH")
        pre_materialization = {
            "resources_tree_sha256": resource_digest,
            "code_resources_sha256": _sha256(code_resources),
            "strict_codesign_valid": True,
        }

    links = sorted(
        (path for path in root.rglob("*") if path.is_symlink()),
        key=lambda item: (-len(item.relative_to(root).parts), item.relative_to(root).as_posix()),
    )
    planned: list[tuple[Path, Path, str]] = []
    for link in links:
        raw_target = os.readlink(link)
        target = link.resolve(strict=True)
        if not _inside(root, target):
            raise MaterializationError(
                f"symlink target escapes app bundle: {link.relative_to(root).as_posix()}"
            )
        if not (target.is_file() or target.is_dir()):
            raise MaterializationError(
                f"unsupported symlink target: {link.relative_to(root).as_posix()}"
            )
        planned.append((link, target, raw_target))

    records: list[dict[str, object]] = []
    for link, target, raw_target in planned:
        if not link.is_symlink():
            # A containing directory link may already have materialized this path.
            continue
        relative = link.relative_to(root).as_posix()
        target_relative = target.relative_to(root).as_posix()
        if (
            signed_profile is not None
            and relative == _SELF_REFERENTIAL_MANIFEST_ALIAS
            and target_relative == _SELF_REFERENTIAL_MANIFEST_TARGET
            and raw_target == _SELF_REFERENTIAL_MANIFEST_RAW_TARGET
        ):
            signed_identity = _object_identity(target)
            link.unlink()
            records.append(
                {
                    "path": relative,
                    "raw_target": raw_target,
                    "target": target_relative,
                    "target_type": "regular",
                    "object_identity": signed_identity,
                    "action": _OMIT_SELF_REFERENTIAL_MANIFEST_ALIAS,
                }
            )
            continue
        parent = link.parent
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{link.name}.materialize.", dir=parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            _copy_tree(target, temporary, root, frozenset())
            link.unlink()
            os.replace(temporary, link)
        finally:
            if temporary.is_symlink() or temporary.is_file():
                temporary.unlink()
            elif temporary.is_dir():
                shutil.rmtree(temporary)
        record: dict[str, object] = {
            "path": relative,
            "raw_target": raw_target,
            "target": target_relative,
            "target_type": "regular" if link.is_file() else "directory",
        }
        records.append(record)

    remaining = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_symlink()]
    if remaining:
        raise MaterializationError(f"symlinks remain after materialization: {remaining[0]}")
    result: dict[str, object] = {
        "schema_id": BOUND_SCHEMA_ID if signed_profile is not None else SCHEMA_ID,
        "app_bundle": root.name,
        "materialized_count": len(records),
        "remaining_symlink_count": 0,
        "records": records,
    }
    if signed_profile is None or signed_profile_path is None or pre_materialization is None:
        for record in records:
            materialized_path = root / str(record["path"])
            if materialized_path.is_file():
                record["sha256"] = _sha256(materialized_path)
                record["size"] = materialized_path.stat().st_size
        return result

    for record in records:
        if record.get("action") == _OMIT_SELF_REFERENTIAL_MANIFEST_ALIAS:
            continue
        materialized_path = root / str(record["path"])
        target_path = root / str(record["target"])
        materialized_identity = _object_identity(materialized_path)
        target_identity = _object_identity(target_path)
        if materialized_identity != target_identity:
            raise MaterializationError("MATERIALIZED_OBJECT_IDENTITY_DRIFT")
        record["object_identity"] = materialized_identity

    first = canonical_app_tree_identity(root)
    second = canonical_app_tree_identity(root)
    if first != second:
        raise MaterializationError("FINAL_APP_TREE_RECOMPUTATION_DRIFT")
    result["signed_stage_profile"] = {
        "filename": signed_profile_path.name,
        "sha256": _sha256(signed_profile_path),
    }
    result["pre_materialization"] = pre_materialization
    result["final_app_tree"] = first
    return result


def validate_materialized_projection(
    app_bundle: Path,
    receipt_path: Path,
    signed_profile_receipt: Path,
) -> dict[str, object]:
    root = app_bundle.resolve(strict=True)
    receipt_file = Path(receipt_path).resolve(strict=True)
    signed_file = Path(signed_profile_receipt).resolve(strict=True)
    if _inside(root, receipt_file) or _inside(root, signed_file):
        raise MaterializationError("MATERIALIZATION_RECEIPT_INSIDE_APP")
    receipt = _read_json(receipt_file, "MATERIALIZATION_RECEIPT_INVALID")
    signed = _read_json(signed_file, "SIGNED_STAGE_PROFILE_RECEIPT_INVALID")
    records = receipt.get("records")
    signed_binding = receipt.get("signed_stage_profile")
    pre = receipt.get("pre_materialization")
    if (
        receipt.get("schema_id") != BOUND_SCHEMA_ID
        or receipt.get("app_bundle") != root.name
        or receipt.get("remaining_symlink_count") != 0
        or type(records) is not list
        or receipt.get("materialized_count") != len(records)
        or type(signed_binding) is not dict
        or signed_binding.get("filename") != signed_file.name
        or signed_binding.get("sha256") != _sha256(signed_file)
        or type(pre) is not dict
    ):
        raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
    signed_checks = signed.get("checks")
    signed_digests = signed.get("digests")
    if (
        signed.get("schema") != 2
        or signed.get("profile") != "external_candidate"
        or signed.get("ok") is not True
        or type(signed_checks) is not dict
        or signed_checks.get("bundle_seal_valid") is not True
        or type(signed_digests) is not dict
        or pre.get("strict_codesign_valid") is not True
        or pre.get("resources_tree_sha256")
        != signed_digests.get("resources_tree_sha256")
        or pre.get("code_resources_sha256")
        != signed_digests.get("code_resources_sha256")
    ):
        raise MaterializationError("SIGNED_STAGE_PROFILE_BINDING_MISMATCH")
    code_resources = root / "Contents/_CodeSignature/CodeResources"
    if (
        not code_resources.is_file()
        or code_resources.is_symlink()
        or _sha256(code_resources) != pre.get("code_resources_sha256")
    ):
        raise MaterializationError("SIGNED_STAGE_CODE_RESOURCES_DRIFT")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise MaterializationError("FINAL_APP_SYMLINK_REMAINS")

    alias_roots: list[str] = []
    seen: set[str] = set()
    omitted_manifest_seen = False
    for record in records:
        if type(record) is not dict:
            raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
        action = record.get("action")
        expected_fields = {
            "path",
            "raw_target",
            "target",
            "target_type",
            "object_identity",
        }
        if action == _OMIT_SELF_REFERENTIAL_MANIFEST_ALIAS:
            expected_fields.add("action")
        if set(record) != expected_fields:
            raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
        relative = str(record["path"])
        target_relative = str(record["target"])
        if relative in seen or not relative or not target_relative:
            raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
        seen.add(relative)
        if action == _OMIT_SELF_REFERENTIAL_MANIFEST_ALIAS:
            if (
                omitted_manifest_seen
                or relative != _SELF_REFERENTIAL_MANIFEST_ALIAS
                or target_relative != _SELF_REFERENTIAL_MANIFEST_TARGET
                or record.get("raw_target")
                != _SELF_REFERENTIAL_MANIFEST_RAW_TARGET
                or record.get("target_type") != "regular"
                or (root / relative).exists()
                or (root / relative).is_symlink()
            ):
                raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
            omitted_manifest_seen = True
            target_path = (root / target_relative).resolve(strict=True)
            if not _inside(root, target_path) or not target_path.is_file():
                raise MaterializationError("MATERIALIZATION_TARGET_DRIFT")
            expected_identity = record.get("object_identity")
            if (
                type(expected_identity) is not dict
                or expected_identity.get("schema_id")
                != "lulu.materialized-object/v1"
                or expected_identity.get("record_count") != 1
                or type(expected_identity.get("sha256")) is not str
                or len(str(expected_identity.get("sha256"))) != 64
            ):
                raise MaterializationError("MATERIALIZATION_RECEIPT_INVALID")
            alias_roots.append(relative)
            continue
        materialized_path = (root / relative).resolve(strict=True)
        target_path = (root / target_relative).resolve(strict=True)
        if not _inside(root, materialized_path) or not _inside(root, target_path):
            raise MaterializationError("MATERIALIZATION_PATH_ESCAPE")
        raw_resolution = (root / relative).parent.joinpath(
            str(record["raw_target"])
        ).resolve(strict=True)
        if not _inside(root, raw_resolution):
            raise MaterializationError("MATERIALIZATION_TARGET_DRIFT")
        expected_type = "regular" if materialized_path.is_file() else "directory"
        if expected_type != record.get("target_type"):
            raise MaterializationError("MATERIALIZATION_TARGET_TYPE_DRIFT")
        expected_identity = record.get("object_identity")
        if (
            _object_identity(materialized_path) != expected_identity
            or _object_identity(target_path) != expected_identity
            or _object_identity(raw_resolution) != expected_identity
        ):
            raise MaterializationError("MATERIALIZED_OBJECT_IDENTITY_DRIFT")
        alias_roots.append(relative)

    if omitted_manifest_seen:
        _verify_component_manifest(
            root,
            root / _SELF_REFERENTIAL_MANIFEST_TARGET,
        )

    first = canonical_app_tree_identity(root)
    second = canonical_app_tree_identity(root)
    if first != second or receipt.get("final_app_tree") != first:
        raise MaterializationError("FINAL_APP_TREE_RECOMPUTATION_DRIFT")
    return {
        "valid": True,
        "alias_roots": sorted(alias_roots, key=lambda value: (value.count("/"), value)),
        "app_tree": first,
        "materialized_count": len(records),
        "materialization_receipt_sha256": _sha256(receipt_file),
        "signed_stage_profile_receipt_sha256": _sha256(signed_file),
        "signed_stage_bundle_seal_valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--signed-profile-receipt", required=True)
    args = parser.parse_args()
    report = materialize(
        Path(args.app),
        signed_profile_receipt=Path(args.signed_profile_receipt),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
