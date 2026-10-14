"""One reproducible identity for a finalized generation-9 Lulu.app tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import unicodedata


SCHEMA_ID = "lulu.canonical-app-tree/v1"
MANIFEST_RELATIVE_PATH = "Contents/Resources/agent_product_manifest.json"


class CanonicalAppTreeError(RuntimeError):
    """The supplied tree cannot be represented by the closed contract."""


def _mode(metadata: os.stat_result) -> str:
    return f"{stat.S_IMODE(metadata.st_mode):04o}"


def _regular_record(root: Path, path: Path, metadata: os.stat_result) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            if (
                actual.st_dev,
                actual.st_ino,
                actual.st_mode,
                actual.st_size,
            ) != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
            ):
                raise CanonicalAppTreeError("APP_TREE_ENTRY_CHANGED_DURING_READ")
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            if size != actual.st_size:
                raise CanonicalAppTreeError("APP_TREE_ENTRY_CHANGED_DURING_READ")
        finally:
            os.close(descriptor)
    except CanonicalAppTreeError:
        raise
    except OSError as exc:
        raise CanonicalAppTreeError("APP_TREE_ENTRY_UNREADABLE") from exc
    return {
        "path": path.relative_to(root).as_posix(),
        "type": "regular",
        "mode": _mode(actual),
        "size": size,
        "sha256": digest.hexdigest(),
    }


def canonical_app_tree_records(app_bundle: Path) -> list[dict[str, object]]:
    """Return the globally sorted, self-reference-free App tree records."""

    root = Path(app_bundle)
    if not root.is_absolute():
        raise CanonicalAppTreeError("APP_TREE_ROOT_NOT_ABSOLUTE")
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise CanonicalAppTreeError("APP_TREE_ROOT_UNREADABLE") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise CanonicalAppTreeError("APP_TREE_ROOT_INVALID")

    records: list[dict[str, object]] = []
    excluded_manifest_seen = False
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise CanonicalAppTreeError("APP_TREE_DIRECTORY_UNREADABLE") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if unicodedata.normalize("NFC", relative) != relative:
                raise CanonicalAppTreeError("APP_TREE_PATH_NOT_NFC")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CanonicalAppTreeError("APP_TREE_ENTRY_UNREADABLE") from exc
            if relative == MANIFEST_RELATIVE_PATH:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise CanonicalAppTreeError("APP_TREE_MANIFEST_INVALID")
                excluded_manifest_seen = True
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise CanonicalAppTreeError("APP_TREE_SYMLINK_FORBIDDEN")
            if stat.S_ISDIR(metadata.st_mode):
                records.append(
                    {"path": relative, "type": "directory", "mode": _mode(metadata)}
                )
                pending.append(path)
                continue
            if stat.S_ISREG(metadata.st_mode):
                records.append(_regular_record(root, path, metadata))
                continue
            raise CanonicalAppTreeError("APP_TREE_SPECIAL_FILE_FORBIDDEN")
    if not excluded_manifest_seen:
        raise CanonicalAppTreeError("APP_TREE_MANIFEST_MISSING")
    return sorted(records, key=lambda item: str(item["path"]).encode("utf-8"))


def canonical_app_tree_identity(app_bundle: Path) -> dict[str, object]:
    records = canonical_app_tree_records(Path(app_bundle))
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    directories = sum(record["type"] == "directory" for record in records)
    regular_files = [record for record in records if record["type"] == "regular"]
    return {
        "schema_id": SCHEMA_ID,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "record_count": len(records),
        "directory_count": directories,
        "regular_file_count": len(regular_files),
        "regular_bytes": sum(int(record["size"]) for record in regular_files),
        "excluded_paths": [MANIFEST_RELATIVE_PATH],
        "included_signing_metadata": True,
        "excluded_metadata": [
            "root_directory_record",
            "uid",
            "gid",
            "mtime",
            "ctime",
            "inode",
            "block_allocation",
            "extended_attributes",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    args = parser.parse_args()
    identity = canonical_app_tree_identity(Path(args.app).resolve())
    print(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
