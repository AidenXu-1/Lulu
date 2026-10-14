#!/usr/bin/env python3
"""Create and independently verify one generation-9 Lulu DMG."""

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
from tempfile import TemporaryDirectory
import unicodedata


SCHEMA_ID = "lulu.generation9-dmg-verification/v1"
MANIFEST_RELATIVE = Path("Contents/Resources/agent_product_manifest.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DmgVerificationError(RuntimeError):
    pass


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        current = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
        ) != (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
        ):
            raise DmgVerificationError("PACKAGE_FILE_IDENTITY_DRIFT")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        return b"".join(chunks), current
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> tuple[str, int, os.stat_result]:
    raw, metadata = _read_regular(path)
    return hashlib.sha256(raw).hexdigest(), len(raw), metadata


def _canonical_app_tree(app: Path) -> dict[str, object]:
    if not app.is_absolute() or app.name != "Lulu.app" or app.is_symlink() or not app.is_dir():
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    records: list[dict[str, object]] = []
    directory_count = 0
    file_count = 0
    regular_bytes = 0
    manifest_seen = False
    for base, directories, names in os.walk(app, topdown=True, followlinks=False):
        root = Path(base)
        for name in directories:
            path = root / name
            relative = path.relative_to(app).as_posix()
            metadata = path.lstat()
            if (
                unicodedata.normalize("NFC", relative) != relative
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
            directory_count += 1
            records.append(
                {
                    "path": relative,
                    "type": "directory",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                }
            )
        for name in names:
            path = root / name
            relative = path.relative_to(app).as_posix()
            if unicodedata.normalize("NFC", relative) != relative:
                raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
            if Path(relative) == MANIFEST_RELATIVE:
                _read_regular(path)
                manifest_seen = True
                continue
            raw, metadata = _read_regular(path)
            file_count += 1
            regular_bytes += len(raw)
            records.append(
                {
                    "path": relative,
                    "type": "regular",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    if not manifest_seen:
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    records.sort(key=lambda value: str(value["path"]).encode("utf-8"))
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "record_count": len(records),
        "directory_count": directory_count,
        "regular_file_count": file_count,
        "regular_bytes": regular_bytes,
    }


def _manifest(app: Path) -> tuple[dict[str, object], str]:
    raw, _metadata = _read_regular(app / MANIFEST_RELATIVE)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH") from exc
    required = {
        "schema_id": "lulu.agent-component-manifest/v1",
        "candidate_generation": 9,
        "contains_secret_material": False,
    }
    if type(payload) is not dict or any(payload.get(key) != value for key, value in required.items()):
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    for field in ("product_version", "product_build", "source_revision", "app_component_set_digest"):
        if type(payload.get(field)) is not str or not payload[field]:
            raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    if not _SHA256.fullmatch(str(payload["app_component_set_digest"])):
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    return payload, hashlib.sha256(raw).hexdigest()


def _identity(path: Path, metadata: os.stat_result) -> dict[str, object]:
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "owner": metadata.st_uid,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _run(arguments: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _mounted_device(raw: bytes, mountpoint: Path) -> str:
    try:
        payload = plistlib.loads(raw)
    except Exception as exc:
        raise DmgVerificationError("PACKAGE_MOUNT_FAILED") from exc
    entities = payload.get("system-entities") if type(payload) is dict else None
    if type(entities) is not list:
        raise DmgVerificationError("PACKAGE_MOUNT_FAILED")
    matches = [
        item
        for item in entities
        if type(item) is dict
        and type(item.get("mount-point")) is str
        and Path(str(item["mount-point"])).resolve() == mountpoint.resolve()
    ]
    if len(matches) != 1 or type(matches[0].get("dev-entry")) is not str:
        raise DmgVerificationError("PACKAGE_MOUNT_FAILED")
    return str(matches[0]["dev-entry"])


def _mount_is_readonly(mountpoint: Path) -> bool:
    result = _run(["/sbin/mount"])
    if result.returncode != 0:
        return False
    marker = f" on {mountpoint.resolve()} (".encode()
    return any(marker in line and b"read-only" in line for line in result.stdout.splitlines())


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_dmg(
    dmg: Path,
    *,
    expected_sha256: str,
    expected_app: Path,
) -> dict[str, object]:
    dmg = Path(dmg)
    expected_app = Path(expected_app)
    if not dmg.is_absolute() or not expected_app.is_absolute() or not _SHA256.fullmatch(expected_sha256):
        raise DmgVerificationError("INVALID_ARGUMENT")
    actual_sha256, dmg_size, dmg_metadata = _file_sha256(dmg)
    if actual_sha256 != expected_sha256:
        raise DmgVerificationError("PACKAGE_HASH_MISMATCH")
    expected_tree = _canonical_app_tree(expected_app)
    expected_manifest, expected_manifest_sha256 = _manifest(expected_app)
    if expected_tree["sha256"] != expected_manifest["app_component_set_digest"]:
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    verify = _run(["/usr/bin/hdiutil", "verify", str(dmg)], timeout=300)
    if verify.returncode != 0:
        raise DmgVerificationError("PACKAGE_HASH_MISMATCH")
    device: str | None = None
    detached = False
    with TemporaryDirectory(prefix="lulu-generation9-mount-") as temporary:
        mountpoint = Path(temporary)
        attach = _run(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-plist",
                "-mountpoint",
                str(mountpoint),
                str(dmg),
            ],
            timeout=120,
        )
        if attach.returncode != 0:
            raise DmgVerificationError("PACKAGE_MOUNT_FAILED")
        try:
            device = _mounted_device(attach.stdout, mountpoint)
            if not _mount_is_readonly(mountpoint):
                raise DmgVerificationError("PACKAGE_MOUNT_NOT_READONLY")
            apps = [path for path in mountpoint.iterdir() if path.name == "Lulu.app"]
            all_apps = [path for path in mountpoint.rglob("*.app")]
            if len(apps) != 1 or len(all_apps) != 1:
                raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
            mounted_app = apps[0]
            mounted_tree = _canonical_app_tree(mounted_app)
            mounted_manifest, mounted_manifest_sha256 = _manifest(mounted_app)
            if (
                mounted_tree != expected_tree
                or mounted_manifest_sha256 != expected_manifest_sha256
                or mounted_manifest != expected_manifest
                or mounted_tree["sha256"] != mounted_manifest["app_component_set_digest"]
            ):
                raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
        finally:
            detach_target = device or str(mountpoint)
            detach = _run(["/usr/bin/hdiutil", "detach", detach_target], timeout=120)
            detached = detach.returncode == 0
        if not detached or mountpoint.is_mount():
            raise DmgVerificationError("PACKAGE_DETACH_FAILED")
    return {
        "schema_id": SCHEMA_ID,
        "verified": True,
        "release_dmg_sha256": actual_sha256,
        "release_dmg_size": dmg_size,
        "release_dmg_identity": _identity(dmg, dmg_metadata),
        "expected_app": str(expected_app),
        "app_component_set_digest": expected_tree["sha256"],
        "component_manifest_sha256": expected_manifest_sha256,
        "product_version": expected_manifest["product_version"],
        "product_build": expected_manifest["product_build"],
        "source_revision": expected_manifest["source_revision"],
        "candidate_generation": 9,
        "mounted_readonly": True,
        "detached": True,
        "contains_secret_material": False,
    }


def create_dmg(app: Path, *, output: Path) -> tuple[str, int]:
    app = Path(app)
    output = Path(output)
    if not app.is_absolute() or not output.is_absolute() or output.exists() or output.is_symlink():
        raise DmgVerificationError("INVALID_ARGUMENT")
    tree = _canonical_app_tree(app)
    manifest, _manifest_sha256 = _manifest(app)
    if tree["sha256"] != manifest["app_component_set_digest"]:
        raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
    expected_name = f"Lulu-{manifest['product_version']}-{manifest['product_build']}-arm64.dmg"
    if output.name != expected_name:
        raise DmgVerificationError("PACKAGE_FILENAME_MISMATCH")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="lulu-generation9-payload-", dir=output.parent) as temporary:
        payload_root = Path(temporary)
        copy = _run(["/usr/bin/ditto", str(app), str(payload_root / "Lulu.app")], timeout=600)
        if copy.returncode != 0 or _canonical_app_tree(payload_root / "Lulu.app") != tree:
            raise DmgVerificationError("PACKAGE_PAYLOAD_MISMATCH")
        create = _run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-volname",
                f"Lulu {manifest['product_version']}",
                "-srcfolder",
                str(payload_root),
                "-format",
                "UDZO",
                str(output),
            ],
            timeout=1800,
        )
        if create.returncode != 0:
            raise DmgVerificationError("PACKAGE_CREATE_FAILED")
    sha256, size, _metadata = _file_sha256(output)
    return sha256, size


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--app", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--receipt", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--dmg", required=True)
    verify.add_argument("--expected-sha256", required=True)
    verify.add_argument("--expected-app", required=True)
    verify.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        receipt = Path(args.receipt)
        if not receipt.is_absolute() or receipt.exists() or receipt.is_symlink():
            raise DmgVerificationError("INVALID_ARGUMENT")
        if args.command == "create":
            app = Path(args.app)
            output = Path(args.output)
            sha256, _size = create_dmg(app, output=output)
            payload = verify_dmg(output, expected_sha256=sha256, expected_app=app)
        else:
            payload = verify_dmg(
                Path(args.dmg),
                expected_sha256=args.expected_sha256,
                expected_app=Path(args.expected_app),
            )
        _write_new_json(receipt, payload)
        json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except (DmgVerificationError, OSError, subprocess.SubprocessError) as exc:
        payload = {
            "schema_id": SCHEMA_ID,
            "verified": False,
            "reason_code": str(exc) or "PACKAGE_VERIFICATION_FAILED",
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 60


if __name__ == "__main__":
    raise SystemExit(main())
