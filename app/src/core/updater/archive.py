from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import stat
import subprocess
import unicodedata
import zipfile

from .constants import MAX_COMPRESSION_RATIO, MAX_SINGLE_UNCOMPRESSED_BYTES, MAX_SYMLINK_DEPTH, MAX_TOTAL_UNCOMPRESSED_BYTES, MAX_ZIP_ENTRIES, MAX_ZIP_PATH_BYTES, MAX_ZIP_PATH_DEPTH
from .models import UpdateError
from .secure_io import ensure_private_dir, read_nofollow


@dataclass(frozen=True)
class ArchiveEntry:
    info: zipfile.ZipInfo
    rel: PurePosixPath
    entry_type: str
    mode: int
    sha256: str = ""
    target: str = ""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_JUNK_NAMES = {"__MACOSX", ".DS_Store", "..namedfork", "com.apple.ResourceFork", "com.apple.FinderInfo"}


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def _normalize_path(value: str, *, allow_parent: bool = False) -> tuple[str, ...]:
    if not value or "\\" in value or "\x00" in value or _has_control(value) or value.startswith(("/", "~")):
        raise UpdateError("zip_path", "安装包结构异常")
    raw = value.split("/")
    if not raw or any(part == "" for part in raw):
        raise UpdateError("zip_path", "安装包结构异常")
    output: list[str] = []
    for part in raw:
        part = unicodedata.normalize("NFC", part)
        if part == "." or (part == ".." and not allow_parent) or (len(part) >= 2 and part[1] == ":"):
            raise UpdateError("zip_path", "安装包结构异常")
        if part == "..":
            output.append(part)
            continue
        if part in _JUNK_NAMES or part.startswith("._"):
            raise UpdateError("zip_junk", "安装包包含系统杂质")
        output.append(part)
    return tuple(output)


def _zip_path(name: str) -> PurePosixPath:
    raw = name[:-1] if name.endswith("/") else name
    parts = _normalize_path(raw)
    if len(parts) > MAX_ZIP_PATH_DEPTH or len("/".join(parts).encode("utf-8")) > MAX_ZIP_PATH_BYTES:
        raise UpdateError("zip_path", "安装包结构异常")
    return PurePosixPath(*parts)


def _entry_mode(info: zipfile.ZipInfo) -> tuple[str, int]:
    raw = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(raw)
    mode = stat.S_IMODE(raw)
    if info.is_dir():
        kind = stat.S_IFDIR
        mode = mode or 0o755
    elif kind == 0:
        kind = stat.S_IFREG
        mode = mode or 0o644
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWGRP | stat.S_IWOTH):
        raise UpdateError("zip_mode", "安装包结构异常")
    if kind == stat.S_IFDIR:
        if mode not in (0o700, 0o755):
            raise UpdateError("zip_mode", "安装包结构异常")
        return "dir", mode
    if kind == stat.S_IFLNK:
        if mode not in (0o700, 0o755):
            raise UpdateError("zip_mode", "安装包结构异常")
        return "symlink", mode
    if kind != stat.S_IFREG:
        raise UpdateError("zip_special", "安装包结构异常")
    executable_root = any(info.filename.startswith(prefix) for prefix in ("Lulu.app/Contents/MacOS/", "Lulu.app/Contents/Helpers/", "Lulu.app/Contents/Frameworks/"))
    if mode & 0o111:
        if not executable_root or mode not in (0o700, 0o755):
            raise UpdateError("zip_mode", "安装包结构异常")
    elif mode not in (0o600, 0o644):
        raise UpdateError("zip_mode", "安装包结构异常")
    return "file", mode


def _target_path(link_path: str, target: str) -> str:
    if not target or len(target.encode("utf-8")) > MAX_ZIP_PATH_BYTES:
        raise UpdateError("zip_link", "安装包链接异常")
    parts = _normalize_path(target, allow_parent=True)
    stack = link_path.split("/")[:-1]
    for part in parts:
        if part == "..":
            if not stack:
                raise UpdateError("zip_link_escape", "安装包链接越界")
            stack.pop()
        else:
            stack.append(part)
    if not stack:
        raise UpdateError("zip_link", "安装包链接异常")
    return "/".join(stack)


def _validate_tree_records(records: list[dict]) -> list[dict]:
    ordered = sorted(records, key=lambda item: unicodedata.normalize("NFC", item["path"]).encode("utf-8"))
    nodes: dict[str, dict] = {}
    folded: set[str] = set()
    for raw in ordered:
        if not isinstance(raw, dict) or raw.get("type") not in {"dir", "file", "symlink"}:
            raise UpdateError("tree_manifest", "安装包树清单异常")
        path = "/".join(_normalize_path(str(raw.get("path", ""))))
        if path.startswith("Lulu.app/") or path == "Lulu.app" or len(path.encode()) > MAX_ZIP_PATH_BYTES:
            raise UpdateError("tree_manifest", "安装包树清单异常")
        folded_path = unicodedata.normalize("NFC", path).casefold()
        if folded_path in folded:
            raise UpdateError("zip_collision", "安装包路径冲突")
        folded.add(folded_path)
        mode = raw.get("mode")
        if type(mode) is not int:
            raise UpdateError("tree_manifest", "安装包树清单异常")
        kind = raw["type"]
        expected = {"path", "type", "mode"} | ({"size", "sha256"} if kind == "file" else {"target"} if kind == "symlink" else set())
        if set(raw) != expected:
            raise UpdateError("tree_manifest", "安装包树清单异常")
        item = {"path": path, "type": kind, "mode": mode}
        if kind == "file":
            size, digest = raw.get("size"), raw.get("sha256")
            if type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise UpdateError("tree_manifest", "安装包树清单异常")
            item.update(size=size, sha256=digest)
        elif kind == "symlink":
            target = unicodedata.normalize("NFC", str(raw.get("target", "")))
            _target_path(path, target)
            item["target"] = target
        nodes[path] = item

    for path, item in nodes.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = nodes.get("/".join(parts[:index]))
            if parent is None or parent["type"] != "dir":
                raise UpdateError("zip_link_parent", "安装包父目录异常")

    def resolve(path: str) -> dict:
        pending = path.split("/")
        current: list[str] = []
        visited: set[str] = set()
        depth = 0
        while pending:
            current.append(pending.pop(0))
            name = "/".join(current)
            node = nodes.get(name)
            if node is None:
                raise UpdateError("zip_link_dangling", "安装包链接悬空")
            if node["type"] == "symlink":
                depth += 1
                if depth > MAX_SYMLINK_DEPTH or name in visited:
                    raise UpdateError("zip_link_cycle", "安装包链接循环或过深")
                visited.add(name)
                target = _target_path(name, node["target"])
                pending = target.split("/") + pending
                current = []
            elif pending and node["type"] != "dir":
                raise UpdateError("zip_link_target", "安装包链接终点异常")
        if node["type"] not in {"dir", "file"}:
            raise UpdateError("zip_link_target", "安装包链接终点异常")
        return node

    for path, item in nodes.items():
        if item["type"] == "symlink":
            resolve(path)
    return [nodes[item["path"]] for item in ordered]


def tree_manifest_bytes(records: list[dict]) -> bytes:
    value = {"schema": 1, "entries": _validate_tree_records(records)}
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def tree_manifest_sha(records: list[dict]) -> str:
    return hashlib.sha256(tree_manifest_bytes(records)).hexdigest()


def _records_from_entries(entries: list[ArchiveEntry]) -> list[dict]:
    records = []
    for entry in entries:
        rel = entry.rel.as_posix()
        if rel == "Lulu.app":
            continue
        path = rel.removeprefix("Lulu.app/")
        item = {"path": path, "type": entry.entry_type, "mode": entry.mode}
        if entry.entry_type == "file":
            item.update(size=entry.info.file_size, sha256=entry.sha256)
        elif entry.entry_type == "symlink":
            item["target"] = entry.target
        records.append(item)
    return _validate_tree_records(records)


def _preflight_archive(archive: zipfile.ZipFile) -> list[ArchiveEntry]:
    infos = archive.infolist()
    if not 1 <= len(infos) <= MAX_ZIP_ENTRIES:
        raise UpdateError("zip_entries", "安装包结构异常")
    seen: set[str] = set()
    total = 0
    output: list[ArchiveEntry] = []
    for info in infos:
        rel = _zip_path(info.filename)
        normalized = rel.as_posix().casefold()
        if normalized in seen:
            raise UpdateError("zip_collision", "安装包路径冲突")
        seen.add(normalized)
        if rel.parts[0] != "Lulu.app":
            raise UpdateError("zip_top", "安装包必须只有Lulu.app顶层")
        entry_type, mode = _entry_mode(info)
        if info.file_size > MAX_SINGLE_UNCOMPRESSED_BYTES:
            raise UpdateError("zip_bomb", "安装包结构异常")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES or info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            raise UpdateError("zip_bomb", "安装包结构异常")
        digest = ""
        target = ""
        if entry_type == "file":
            h = hashlib.sha256()
            with archive.open(info) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    h.update(block)
            digest = h.hexdigest()
        elif entry_type == "symlink":
            if info.file_size > MAX_ZIP_PATH_BYTES:
                raise UpdateError("zip_link", "安装包链接异常")
            try:
                payload = archive.read(info)
                target = unicodedata.normalize("NFC", payload.decode("utf-8", errors="strict"))
            except Exception as exc:
                raise UpdateError("zip_link", "安装包链接编码异常") from exc
        output.append(ArchiveEntry(info, rel, entry_type, mode, digest, target))
    required = {"lulu.app/contents/macos/lulu", "lulu.app/contents/info.plist"}
    if not required <= seen or "lulu.app" not in seen:
        raise UpdateError("zip_missing", "安装包结构异常")
    main = next(item for item in output if item.rel.as_posix() == "Lulu.app/Contents/MacOS/Lulu")
    if main.entry_type != "file" or not main.mode & stat.S_IXUSR:
        raise UpdateError("zip_mode", "安装包结构异常")
    _records_from_entries(output)
    return output


def preflight_zip(path: Path) -> list[ArchiveEntry]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle, zipfile.ZipFile(handle) as archive:
            return _preflight_archive(archive)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("zip_invalid", "安装包结构异常") from exc


def _open_existing_dir_chain(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except Exception as exc:
        os.close(current)
        raise UpdateError("extract_race", "安装准备目录异常") from exc


def _remove_dir_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        st = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
            try:
                _remove_dir_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise UpdateError("cleanup_special", "安装清理路径包含特殊文件")


def _remove_staged(path: Path) -> None:
    try:
        parent_fd = os.open(path.parent, _DIR_FLAGS)
        try:
            st = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise UpdateError("cleanup_path", "安装清理路径不安全")
            child = os.open(path.name, _DIR_FLAGS, dir_fd=parent_fd)
            try:
                _remove_dir_contents(child)
            finally:
                os.close(child)
            os.rmdir(path.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except FileNotFoundError:
        return


def extract_zip(path: Path, staged_root: Path, *, expected_tree_sha: str | None = None) -> Path:
    ensure_private_dir(staged_root.parent)
    try:
        staged_root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise UpdateError("staged_exists", "安装准备目录异常") from exc
    total = 0
    zip_fd = root_fd = None
    try:
        zip_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        root_fd = os.open(staged_root, _DIR_FLAGS)
        with os.fdopen(zip_fd, "rb", closefd=False) as zip_handle, zipfile.ZipFile(zip_handle) as archive:
            entries = _preflight_archive(archive)
            records = _records_from_entries(entries)
            archive_tree_sha = tree_manifest_sha(records)
            if expected_tree_sha and archive_tree_sha != expected_tree_sha:
                raise UpdateError("tree_manifest", "安装包树校验失败")
            directories = sorted((item for item in entries if item.entry_type == "dir"), key=lambda item: (len(item.rel.parts), item.rel.as_posix()))
            files = sorted((item for item in entries if item.entry_type == "file"), key=lambda item: item.rel.as_posix())
            links = sorted((item for item in entries if item.entry_type == "symlink"), key=lambda item: item.rel.as_posix())
            for entry in directories:
                parent_fd = _open_existing_dir_chain(root_fd, entry.rel.parts[:-1])
                try:
                    os.mkdir(entry.rel.name, entry.mode, dir_fd=parent_fd)
                    st = os.stat(entry.rel.name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != entry.mode:
                        raise UpdateError("extract_mode", "安装准备目录权限异常")
                finally:
                    os.close(parent_fd)
            for entry in files:
                parent_fd = _open_existing_dir_chain(root_fd, entry.rel.parts[:-1])
                try:
                    fd = os.open(entry.rel.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), entry.mode, dir_fd=parent_fd)
                    h = hashlib.sha256()
                    size = 0
                    try:
                        # The finally block owns this fd even if write/flush
                        # fails. A second close would hide ENOSPC with EBADF.
                        with os.fdopen(fd, "wb", closefd=False) as destination, archive.open(entry.info) as source:
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                size += len(block)
                                total += len(block)
                                if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                                    raise UpdateError("zip_bomb", "安装包结构异常")
                                h.update(block)
                                destination.write(block)
                    finally:
                        os.close(fd)
                    st = os.stat(entry.rel.name, dir_fd=parent_fd, follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != entry.mode or size != entry.info.file_size or h.hexdigest() != entry.sha256:
                        raise UpdateError("extract_file", "安装文件校验失败")
                finally:
                    os.close(parent_fd)
            for entry in links:
                parent_fd = _open_existing_dir_chain(root_fd, entry.rel.parts[:-1])
                try:
                    parent_before = os.fstat(parent_fd)
                    os.symlink(entry.target, entry.rel.name, dir_fd=parent_fd)
                    st = os.stat(entry.rel.name, dir_fd=parent_fd, follow_symlinks=False)
                    target = os.readlink(entry.rel.name, dir_fd=parent_fd)
                    parent_after = os.fstat(parent_fd)
                    if (parent_before.st_dev, parent_before.st_ino) != (parent_after.st_dev, parent_after.st_ino) or not stat.S_ISLNK(st.st_mode) or stat.S_IMODE(st.st_mode) != entry.mode or unicodedata.normalize("NFC", target) != entry.target:
                        raise UpdateError("extract_link", "安装链接创建异常")
                finally:
                    os.close(parent_fd)
        app = staged_root / "Lulu.app"
        disk_records, disk_sha = app_file_manifest(app)
        if disk_sha != archive_tree_sha or disk_records != records:
            raise UpdateError("tree_manifest", "安装包树校验失败")
        return app
    except Exception:
        try:
            _remove_staged(staged_root)
        except (OSError, UpdateError):
            # Preserve leftovers for the bounded startup cleanup; do not replace
            # the download/extraction failure with a secondary cleanup failure.
            pass
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if zip_fd is not None:
            os.close(zip_fd)


def app_file_manifest(app: Path) -> tuple[list[dict], str]:
    root_st = app.lstat()
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        raise UpdateError("app_structure", "安装包结构异常")
    records: list[dict] = []
    for current, dirs, files in os.walk(app, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.sort()
        files.sort()
        retained = []
        for name in dirs:
            path = current_path / name
            st = path.lstat()
            rel = path.relative_to(app).as_posix()
            if stat.S_ISLNK(st.st_mode):
                records.append({"path": rel, "type": "symlink", "mode": stat.S_IMODE(st.st_mode), "target": unicodedata.normalize("NFC", os.readlink(path))})
            elif stat.S_ISDIR(st.st_mode):
                records.append({"path": rel, "type": "dir", "mode": stat.S_IMODE(st.st_mode)})
                retained.append(name)
            else:
                raise UpdateError("app_special", "安装包结构异常")
        dirs[:] = retained
        for name in files:
            path = current_path / name
            st = path.lstat()
            rel = path.relative_to(app).as_posix()
            if stat.S_ISLNK(st.st_mode):
                records.append({"path": rel, "type": "symlink", "mode": stat.S_IMODE(st.st_mode), "target": unicodedata.normalize("NFC", os.readlink(path))})
            elif stat.S_ISREG(st.st_mode):
                digest = hashlib.sha256(read_nofollow(path)).hexdigest()
                records.append({"path": rel, "type": "file", "mode": stat.S_IMODE(st.st_mode), "size": st.st_size, "sha256": digest})
            else:
                raise UpdateError("app_special", "安装包结构异常")
    records = _validate_tree_records(records)
    return records, tree_manifest_sha(records)


def _zip_info(name: str, kind: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if kind == "dir" and not name.endswith("/") else ""), date_time=(2020, 1, 1, 0, 0, 0))
    info.create_system = 3
    file_type = stat.S_IFDIR if kind == "dir" else stat.S_IFLNK if kind == "symlink" else stat.S_IFREG
    info.external_attr = (file_type | mode) << 16
    if kind == "dir":
        info.external_attr |= 0x10
    info.compress_type = zipfile.ZIP_STORED
    return info


def build_update_zip(app: Path, destination: Path) -> tuple[list[dict], str]:
    app = app.resolve(strict=True)
    records, expected_sha = app_file_manifest(app)
    root_mode = stat.S_IMODE(app.lstat().st_mode)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w+b", closefd=True) as handle, zipfile.ZipFile(handle, "w", allowZip64=True) as archive:
            archive.writestr(_zip_info("Lulu.app", "dir", root_mode), b"")
            for record in records:
                name = "Lulu.app/" + record["path"]
                info = _zip_info(name, record["type"], record["mode"])
                if record["type"] == "dir":
                    archive.writestr(info, b"")
                elif record["type"] == "symlink":
                    archive.writestr(info, record["target"].encode("utf-8"))
                else:
                    source_fd = os.open(app / record["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        with archive.open(info, "w", force_zip64=True) as target:
                            for block in iter(lambda: os.read(source_fd, 1024 * 1024), b""):
                                target.write(block)
                    finally:
                        os.close(source_fd)
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    entries = preflight_zip(destination)
    actual_records = _records_from_entries(entries)
    if actual_records != records or tree_manifest_sha(actual_records) != expected_sha:
        destination.unlink(missing_ok=True)
        raise UpdateError("tree_manifest", "发布ZIP树校验失败")
    return records, expected_sha


def validate_staged_app(app: Path, *, version: str, build: str, verify_system: bool = True) -> str:
    for rel, kind in (("", "dir"), ("Contents", "dir"), ("Contents/MacOS", "dir"), ("Contents/MacOS/Lulu", "file"), ("Contents/Info.plist", "file")):
        path = app / rel
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or (kind == "dir") != stat.S_ISDIR(st.st_mode):
            raise UpdateError("app_structure", "安装包结构异常")
    try:
        info = plistlib.loads(read_nofollow(app / "Contents/Info.plist", maximum=1024 * 1024))
    except Exception as exc:
        raise UpdateError("app_identity", "安装包版本异常") from exc
    if info.get("CFBundleExecutable") != "Lulu" or info.get("CFBundleShortVersionString") != version or info.get("CFBundleVersion") != build:
        raise UpdateError("app_identity", "安装包版本异常")
    if verify_system:
        try:
            arch = subprocess.run(["/usr/bin/lipo", "-archs", str(app / "Contents/MacOS/Lulu")], capture_output=True, timeout=10, check=False)
            seal = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, timeout=30, check=False)
        except Exception as exc:
            raise UpdateError("app_verify", "安装包安全校验失败") from exc
        if arch.returncode != 0 or b"arm64" not in arch.stdout.split() or seal.returncode != 0:
            raise UpdateError("app_verify", "安装包安全校验失败")
    return app_file_manifest(app)[1]
