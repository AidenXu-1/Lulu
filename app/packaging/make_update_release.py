#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.updater.archive import app_file_manifest, build_update_zip
from src.version import channel_for_profile

VERSION = runpy.run_path(str(ROOT / "src/version.py"))
PROFILE_GATE = runpy.run_path(str(ROOT / "packaging/profile_gate.py"))
ALLOWED_RELEASE_REPOSITORIES = ("AidenXu-1/Lulu", "AidenXu-1/lulu-releases")


def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()


def write_exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_update_app(source: Path, destination: Path) -> None:
    app_file_manifest(source)
    shutil.copytree(source, destination, symlinks=True)
    from src.core.agent_entry.standalone_source import DESCRIPTOR, inspect_install_source
    if (source/DESCRIPTOR).exists():
        if inspect_install_source(source) != inspect_install_source(destination):
            raise SystemExit('更新 ZIP 与 DMG 的 App 内容不一致')
        return
    allowed_exec = ("Contents/MacOS/", "Contents/Helpers/", "Contents/Frameworks/")
    for current, dirs, files in os.walk(destination, topdown=True, followlinks=False):
        current_path = Path(current)
        retained = []
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                continue
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise SystemExit("发布staging包含特殊目录")
            os.chmod(path, 0o755)
            retained.append(name)
        dirs[:] = retained
        for name in files:
            path = current_path / name
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise SystemExit("发布staging包含特殊文件")
            rel = path.relative_to(destination).as_posix()
            mode = 0o755 if (st.st_mode & 0o111) and rel.startswith(allowed_exec) else 0o644
            os.chmod(path, mode)
    app_file_manifest(destination)
    subprocess.run(["/usr/bin/codesign","--force","--deep","--sign","-",str(destination)],check=True)
    subprocess.run(["/usr/bin/codesign","--verify","--deep","--strict",str(destination)],check=True)


def update_release_channel(profile: dict) -> str:
    if profile.get("profile") != "external_candidate" or profile.get("upload_target") != "github-release-candidate":
        raise SystemExit("应用内更新资产只接受 external_candidate 构建身份")
    try:
        channel = channel_for_profile(profile.get("profile"))
    except RuntimeError as exc:
        raise SystemExit("应用内更新资产缺少受支持的发布通道") from exc
    declared_channel = profile.get("channel")
    if channel != "production" or (declared_channel is not None and declared_channel != channel):
        raise SystemExit("应用内更新资产必须封存为 production 通道")
    return channel


def main():
    p=argparse.ArgumentParser(); p.add_argument("--app",required=True); p.add_argument("--dmg",required=True); p.add_argument("--output",required=True); p.add_argument("--private-key",required=True); p.add_argument("--notes",required=True); p.add_argument("--repository",choices=ALLOWED_RELEASE_REPOSITORIES,required=True); args=p.parse_args()
    raw_app=Path(args.app).expanduser(); raw_dmg=Path(args.dmg).expanduser(); raw_key=Path(args.private_key).expanduser(); raw_notes=Path(args.notes).expanduser()
    if not args.private_key:
        raise SystemExit("更新签名私钥必须通过项目外显式路径提供")
    for path,label,kind in ((raw_app,"App",stat.S_IFDIR),(raw_dmg,"DMG",stat.S_IFREG),(raw_key,"更新签名私钥",stat.S_IFREG),(raw_notes,"更新说明",stat.S_IFREG)):
        try: value=path.lstat()
        except FileNotFoundError: raise SystemExit(f"{label}不存在")
        if stat.S_ISLNK(value.st_mode) or stat.S_IFMT(value.st_mode)!=kind:
            raise SystemExit(f"{label}必须是非符号链接的{'目录' if kind==stat.S_IFDIR else '普通文件'}")
    app=raw_app.resolve(); dmg=raw_dmg.resolve(); out=Path(args.output).resolve(); key=raw_key.resolve(); notes_path=raw_notes.resolve()
    profile = PROFILE_GATE["read_app_profile"](app)
    channel = update_release_channel(profile)
    if ROOT in key.parents:
        raise SystemExit("更新签名私钥必须通过项目外显式路径提供")
    st=key.lstat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)&0o077:
        raise SystemExit("更新签名私钥权限必须为当前用户私有")
    notes=notes_path.read_text(encoding="utf-8")
    if len(notes.encode())>20000 or any(len(line.encode())>1000 for line in notes.splitlines()) or "<" in notes or ">" in notes:
        raise SystemExit("更新说明超出安全纯文本规则")
    version=VERSION["__version__"]; build=VERSION["__build__"]
    zip_name=f"Lulu-{version}-{build}-arm64.zip"; dmg_name=f"Lulu-{version}-{build}-arm64.dmg"; zip_path=out/zip_name; canonical_dmg=out/dmg_name
    out.mkdir(parents=True,exist_ok=True)
    # Keep audit copies outside the public upload directory.
    evidence = out.parent / ('.' + out.name + '-release-evidence')
    evidence.mkdir(mode=0o700)
    subprocess.run(["/usr/bin/codesign","--verify","--deep","--strict",str(app)],check=True)
    with tempfile.TemporaryDirectory(prefix="lulu-update-release-",dir=out) as temp:
        prepared=Path(temp)/"Lulu.app"
        prepare_update_app(app,prepared)
        stored_zip = Path(temp)/'update-stored.zip'
        tree_records, tree_sha = build_update_zip(prepared, stored_zip)
        # Compression affects transfer size only; old updater tree/signature
        # checks see exactly the same App bytes and permissions.
        import importlib.util
        spec = importlib.util.spec_from_file_location('lulu_zip_compressor', Path(__file__).with_name('compress_update_zip.py'))
        compressor = importlib.util.module_from_spec(spec); spec.loader.exec_module(compressor)
        compressor.compress_update_zip(stored_zip, zip_path)
    tree_path = evidence / "tree-manifest.json"
    write_exclusive(tree_path,(json.dumps({"schema":1,"entries":tree_records},ensure_ascii=False,separators=(",",":"))+"\n").encode())
    if dmg!=canonical_dmg:
        with dmg.open("rb") as source, canonical_dmg.open("xb") as target:
            shutil.copyfileobj(source,target,1024*1024)
        os.chmod(canonical_dmg,0o600)
    write_exclusive(evidence/(zip_name+".sha256"),f"{digest(zip_path)}  {zip_name}\n".encode())
    repository=args.repository
    from src.core.agent_entry.standalone_source import DESCRIPTOR
    from src.core.updater.constants import MINIMUM_AUTO_UPDATE_VERSION
    minimum_version = MINIMUM_AUTO_UPDATE_VERSION if (app/DESCRIPTOR).exists() else "0.2.2"
    from src.core.updater.manifest import version_tuple
    if version_tuple(version) < version_tuple(minimum_version):
        raise SystemExit('候选版本低于已验证的自动升级边界，拒绝生成更新清单')
    manifest={"schema":1,"app":"Lulu","channel":channel,"latest_version":version,"latest_build":build,"min_supported_version":minimum_version,"key_id":"lulu-update-rsa3072-2026-07","mandatory":False,"published_at":datetime.now(timezone.utc).isoformat(),"notes_md":notes,"assets":{"macos_arm64":{"kind":"zip","filename":zip_name,"url":f"https://github.com/{repository}/releases/download/v{version}/{zip_name}","sha256":digest(zip_path),"size":zip_path.stat().st_size,"tree_manifest_sha256":tree_sha},"macos_arm64_dmg":{"kind":"dmg","filename":dmg_name,"url":f"https://github.com/{repository}/releases/download/v{version}/{dmg_name}","sha256":digest(canonical_dmg),"size":canonical_dmg.stat().st_size}}}
    raw=(json.dumps(manifest,ensure_ascii=False,separators=(",",":"))+"\n").encode(); manifest_path=out/"version.json"; write_exclusive(manifest_path,raw)
    with tempfile.TemporaryDirectory(prefix="lulu-update-sign-") as temp:
        sig=Path(temp)/"sig.bin"; subprocess.run(["/usr/bin/openssl","dgst","-sha256","-sign",str(key),"-out",str(sig),str(manifest_path)],check=True,timeout=10)
        write_exclusive(out/"version.json.sig",base64.b64encode(sig.read_bytes())+b"\n")
    write_exclusive(evidence/"release-notes.md",notes.encode())


if __name__=="__main__": main()
