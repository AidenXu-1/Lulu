"""Give a verified source ZIP the same local Git checks as a normal checkout.

The generated commit describes these exact archive bytes. It is a local build
snapshot, not a claim to be the maintainer's release commit. No network, global
Git configuration, signing key, or preinstalled Lulu is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

ARCHIVE_BASELINE = "refs/lulu/source-archive-baseline"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _git(root, *arguments, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="Lulu source archive", GIT_AUTHOR_EMAIL="source@lulu.invalid",
               GIT_COMMITTER_NAME="Lulu source archive", GIT_COMMITTER_EMAIL="source@lulu.invalid",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000")
    result = subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null",
        "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
        input=input, capture_output=True, env=env)
    if result.returncode:
        raise SystemExit("无法准备本地源码记录：" + result.stderr.decode(errors="replace"))
    return result.stdout.decode().strip()


def _regular(root, relative):
    parts = PurePosixPath(relative)
    if (not relative or parts.is_absolute() or parts.as_posix() != relative
            or any(p in (".", "..", ".git") for p in parts.parts)
            or "\0" in relative or "\n" in relative or "\t" in relative):
        raise SystemExit("源码清单路径无效")
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink():
            raise SystemExit("源码清单不接受符号链接：" + relative)
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise SystemExit("源码缺少文件：" + relative)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise SystemExit("源码清单不是普通文件：" + relative)
    return path


def verify_archive(root):
    root = Path(root).resolve()
    manifest_path = _regular(root, "SOURCE_MANIFEST.json")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (ValueError, UnicodeError):
        raise SystemExit("源码清单无法读取，请重新下载完整源码包")
    if (type(manifest) is not dict or manifest.get("schema") != 2
            or type(manifest.get("files")) is not list or not manifest["files"]):
        raise SystemExit("源码清单版本无效，请重新下载完整源码包")
    records = []
    seen = {"SOURCE_MANIFEST.json"}
    for entry in manifest["files"]:
        if (type(entry) is not dict or set(entry) != {"path", "size", "sha256", "mode"}
                or type(entry["path"]) is not str or entry["path"] in seen
                or type(entry["size"]) is not int or entry["size"] < 0
                or type(entry["sha256"]) is not str or not _SHA256.fullmatch(entry["sha256"])
                or entry["mode"] not in ("100644", "100755")):
            raise SystemExit("源码清单条目无效")
        seen.add(entry["path"])
        path = _regular(root, entry["path"])
        raw = path.read_bytes()
        if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise SystemExit("源码文件与清单不符：" + entry["path"])
        records.append((entry["path"], entry["mode"], raw))
    # Record the manifest too; unlike embedding a commit inside itself, this has
    # no recursive hash problem and binds all build inputs to real Git objects.
    records.append(("SOURCE_MANIFEST.json", "100644", manifest_path.read_bytes()))
    return records


def prepare_repository(root):
    root = Path(root).resolve()
    git_path = root / ".git"
    if os.path.lexists(git_path):
        if git_path.is_symlink() or _git(root, "rev-parse", "--show-toplevel") != str(root):
            raise SystemExit("源码记录位置无效，保留现场，请使用独立源码目录")
        return
    records = verify_archive(root)  # Refuse tampered/incomplete archives before creating anything.
    _git(root, "init", "--quiet", "--template=", "--object-format=sha1", "--initial-branch=source-archive")
    index = []
    for name, mode, raw in records:
        oid = _git(root, "hash-object", "--no-filters", "-w", "--stdin", input=raw)
        index.append(f"{mode} {oid}\t{name}\0")
        # GitHub ZIPs omit Unix modes. Restore only modes declared by the checked
        # manifest, so the resulting worktree is clean on both ZIP and tar.gz.
        os.chmod(root / name, 0o755 if mode == "100755" else 0o644)
    _git(root, "update-index", "-z", "--index-info", input="".join(index).encode())
    tree = _git(root, "write-tree")
    commit = _git(root, "commit-tree", tree,
                  input=b"Local build snapshot from Lulu source archive\n")
    _git(root, "update-ref", "refs/heads/source-archive", commit)
    _git(root, "update-ref", ARCHIVE_BASELINE, commit)
    print("源码包校验通过，已建立仅用于本地构建的版本记录")


def default_baseline(root, published_baseline):
    # An existing normal checkout continues to use its public accepted ancestor.
    # Only archives prepared above have this local reference.
    result = subprocess.run(["/usr/bin/git", "-C", str(root), "show-ref", "--verify",
                             "--quiet", ARCHIVE_BASELINE], capture_output=True,
                            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    return ARCHIVE_BASELINE if result.returncode == 0 else published_baseline


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    prepare_repository(args.repo)
