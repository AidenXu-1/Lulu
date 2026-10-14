"""Versioned complete signed-App identity; no in-App self-referential exclusions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import unicodedata

SCHEMA_ID = "lulu.canonical-app-tree/v2"
LINK_SCHEMA = "lulu.signed-app-links/v2"
LINK_POLICY = "Contents/Resources/agent_links.v2.json"
DELIVERY_MARKER = "Contents/Resources/component_delivery.v2.json"
MANIFEST_NAME = "Lulu.components.v2.json"
MANIFEST_SCHEMA = "lulu.agent-component-manifest/v2"
DMG_SCHEMA = "lulu.generation9-dmg-verification/v2"
MARKER_PAYLOAD = {"schema_id": "lulu.external-component-delivery/v2",
                  "manifest_name": MANIFEST_NAME, "tree_schema": SCHEMA_ID}


def strict_json(raw: bytes):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def verify_delivery_marker(app: Path) -> None:
    path = _root(app) / DELIVERY_MARKER
    if path.is_symlink() or not path.is_file() or strict_json(path.read_bytes()) != MARKER_PAYLOAD:
        raise _fail("APP_DELIVERY_MARKER_REQUIRED")


class DeliveryIdentityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.reason_code = code
        self.state = "update_required"
        self.effect_count = 0


def _fail(code="AGENT_ENTRY_COMPONENT_MISMATCH"):
    return DeliveryIdentityError(code)


def _root(app: Path) -> Path:
    app = Path(app)
    if not app.is_absolute() or app.name != "Lulu.app":
        raise _fail()
    metadata = app.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise _fail()
    return app


def _stamp(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _file_record(root: Path, path: Path, expected: os.stat_result):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        actual = os.fstat(fd)
        if _stamp(actual) != _stamp(expected) or not stat.S_ISREG(actual.st_mode):
            raise _fail()
        digest = hashlib.sha256()
        size = 0
        while block := os.read(fd, 1024 * 1024):
            size += len(block)
            digest.update(block)
        if size != actual.st_size or _stamp(os.fstat(fd)) != _stamp(actual):
            raise _fail()
    finally:
        os.close(fd)
    return {"path": path.relative_to(root).as_posix(), "type": "regular",
            "mode": f"{stat.S_IMODE(actual.st_mode):04o}", "size": size,
            "sha256": digest.hexdigest()}


def _link_record(root: Path, path: Path):
    before = path.lstat()
    raw = os.readlink(path)
    if not raw or Path(raw).is_absolute():
        raise _fail("APP_LINK_TARGET_FORBIDDEN")
    try:
        target = path.resolve(strict=True)
        relative = target.relative_to(root.resolve(strict=True)).as_posix()
    except (ValueError, OSError, RuntimeError) as exc:
        raise _fail("APP_LINK_TARGET_FORBIDDEN") from exc
    if relative == "." or _stamp(path.lstat()) != _stamp(before):
        raise _fail("APP_LINK_TARGET_FORBIDDEN")
    metadata = target.lstat()
    kind = "regular" if stat.S_ISREG(metadata.st_mode) else "directory" if stat.S_ISDIR(metadata.st_mode) else None
    if kind is None:
        raise _fail("APP_LINK_TARGET_FORBIDDEN")
    return {"path": path.relative_to(root).as_posix(), "raw_target": raw,
            "resolved_target": relative, "target_type": kind}


def _inventory(app: Path):
    root = _root(app)
    records, links = [], []
    pending = [root]
    while pending:
        base = pending.pop()
        with os.scandir(base) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if unicodedata.normalize("NFC", relative) != relative:
                    raise _fail()
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    link = _link_record(root, path)
                    links.append(link)
                    records.append(dict(link, type="symlink", mode=f"{stat.S_IMODE(metadata.st_mode):04o}"))
                elif stat.S_ISDIR(metadata.st_mode):
                    records.append({"path": relative, "type": "directory", "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"})
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    records.append(_file_record(root, path, metadata))
                else:
                    raise _fail()
    key = lambda r: r["path"].encode("utf-8")
    return sorted(records, key=key), sorted(links, key=key)


def delivery_app_tree_records(app: Path) -> list[dict]:
    root = _root(app)
    policy_path = root / LINK_POLICY
    if policy_path.is_symlink() or not policy_path.is_file():
        raise _fail("APP_LINK_POLICY_REQUIRED")
    raw = policy_path.read_bytes()
    try:
        policy = strict_json(raw)
    except (ValueError, UnicodeError) as exc:
        raise _fail("APP_LINK_POLICY_REQUIRED") from exc
    records, links = _inventory(root)
    if (type(policy) is not dict or set(policy) != {"schema_id", "links"}
            or policy.get("schema_id") != LINK_SCHEMA or policy.get("links") != links):
        raise _fail("APP_LINK_POLICY_MISMATCH")
    # The file read for policy interpretation must be the same bytes hashed in
    # the complete tree, and that tree includes all final signature metadata.
    actual = next((r for r in records if r["path"] == LINK_POLICY), None)
    if not actual or actual.get("sha256") != hashlib.sha256(raw).hexdigest():
        raise _fail("APP_LINK_POLICY_MISMATCH")
    return records


def delivery_app_tree_identity(app: Path) -> dict:
    records = delivery_app_tree_records(app)
    raw = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    regular = [r for r in records if r["type"] == "regular"]
    return {"schema_id": SCHEMA_ID, "sha256": hashlib.sha256(raw).hexdigest(),
            "record_count": len(records), "directory_count": sum(r["type"] == "directory" for r in records),
            "regular_file_count": len(regular), "regular_bytes": sum(r["size"] for r in regular),
            "symlink_count": sum(r["type"] == "symlink" for r in records),
            "excluded_paths": [], "included_signing_metadata": True}


def verify_final_signature(app: Path) -> dict:
    root = _root(app)
    results = {}
    for label, options in (("strict", ["--strict"]), ("deep_strict", ["--deep", "--strict"])):
        result = subprocess.run(["/usr/bin/codesign", "--verify", *options, str(root)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
        if result.returncode != 0:
            raise _fail("FINAL_APP_SIGNATURE_INVALID")
        results[label] = True
    return results
