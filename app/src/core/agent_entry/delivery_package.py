"""Read-only verification of one DMG carrying a final signed App and external manifest."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import stat
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from .component_manifest import verify_component_manifest
from .delivery_identity import MANIFEST_NAME, DMG_SCHEMA, delivery_app_tree_identity


@dataclass(frozen=True)
class VerifiedDelivery:
    receipt: dict
    manifest_bytes: bytes
    staged_app: Path


def _stamp(meta):
    return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size,
            meta.st_mtime_ns, meta.st_ctime_ns)


def _read(path: Path, *, digest_only=False):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        current = os.fstat(fd)
        if _stamp(before) != _stamp(current):
            raise RuntimeError('PACKAGE_FILE_IDENTITY_DRIFT')
        digest = hashlib.sha256()
        blocks = []
        while block := os.read(fd, 1024 * 1024):
            if digest_only: digest.update(block)
            else: blocks.append(block)
        if _stamp(os.fstat(fd)) != _stamp(before) or _stamp(path.lstat()) != _stamp(before):
            raise RuntimeError('PACKAGE_FILE_IDENTITY_DRIFT')
        return (digest.hexdigest() if digest_only else b''.join(blocks)), current
    finally:
        os.close(fd)


def _run(args,timeout=120):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout,check=False)


def _verify_install_action(path):
    if path.is_symlink() or not path.is_dir():raise RuntimeError('PACKAGE_INSTALL_ACTION_REQUIRED')
    for base,dirs,files in os.walk(path,followlinks=False):
        if any((Path(base)/name).is_symlink() for name in (*dirs,*files)):
            raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
    info=plistlib.loads(_read(path/'Contents/Info.plist')[0])
    if info.get('CFBundleExecutable')!='InstallLulu' or info.get('CFBundleIdentifier')!='com.lulu.transcribe.installer':
        raise RuntimeError('PACKAGE_INSTALL_ACTION_REQUIRED')
    for flags in (['--strict'],['--deep','--strict']):
        result=subprocess.run(['/usr/bin/codesign','--verify',*flags,str(path)],capture_output=True,timeout=120)
        if result.returncode:raise RuntimeError('PACKAGE_INSTALL_ACTION_INVALID')


def verify_delivery_dmg(dmg: Path, *, expected_app: Path, expected_sha256: str, stage_root: Path) -> VerifiedDelivery:
    """Stage only verified mounted bytes; no installed state is written. Detach is mandatory."""
    dmg=Path(dmg);app=Path(expected_app);stage=Path(stage_root)
    if type(expected_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("PACKAGE_INDEPENDENT_HASH_REQUIRED")
    if not stage.is_absolute() or os.path.lexists(stage):
        raise RuntimeError("PACKAGE_STAGE_MUST_BE_NEW")
    if not dmg.is_absolute() or not app.is_absolute() or dmg.suffix.lower()!='.dmg':
        raise RuntimeError('INVALID_ARGUMENT')
    digest,metadata=_read(dmg,digest_only=True)
    if digest!=expected_sha256:raise RuntimeError("PACKAGE_HASH_MISMATCH")
    identity=delivery_app_tree_identity(app)
    check=_run(['/usr/bin/hdiutil','verify',str(dmg)],300)
    if check.returncode:raise RuntimeError('PACKAGE_HASH_MISMATCH')
    with TemporaryDirectory(prefix='lulu-external-delivery-') as tmp:
        mount=Path(tmp)
        result=_run(['/usr/bin/hdiutil','attach','-readonly','-nobrowse','-plist','-mountpoint',str(mount),str(dmg)])
        if result.returncode:raise RuntimeError('PACKAGE_MOUNT_FAILED')
        device=None
        try:
            info=plistlib.loads(result.stdout)
            matches=[x for x in info.get('system-entities',[]) if x.get('mount-point') and Path(x['mount-point']).resolve()==mount.resolve()]
            if len(matches)!=1 or not isinstance(matches[0].get('dev-entry'),str):raise RuntimeError('PACKAGE_MOUNT_FAILED')
            device=matches[0]['dev-entry']
            mounts=_run(['/sbin/mount'])
            marker=f' on {mount.resolve()} ('.encode()
            if mounts.returncode or not any(marker in line and b'read-only' in line for line in mounts.stdout.splitlines()):
                raise RuntimeError('PACKAGE_MOUNT_NOT_READONLY')
            mounted=mount/'Lulu.app'
            # The installer is a distribution action, not another Lulu product.
            names={p.name for p in mount.iterdir()}
            if not {'Lulu.app',MANIFEST_NAME,'Install Lulu.app'}.issubset(names) or names-{'Lulu.app',MANIFEST_NAME,'Install Lulu.app','.DS_Store','.Trashes','.fseventsd','.VolumeIcon.icns'}:
                raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
            if mounted.is_symlink() or not mounted.is_dir() or mounted.resolve().parent!=mount.resolve():
                raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
            _verify_install_action(mount/'Install Lulu.app')
            manifest_path=mount/MANIFEST_NAME
            manifest_bytes,_=_read(manifest_path)
            proof=verify_component_manifest(mounted,manifest_path)
            if delivery_app_tree_identity(mounted)!=identity:raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
            # Verify the source/installed App against the same external manifest,
            # including both native signature checks, rather than trusting its name.
            if verify_component_manifest(app,manifest_path)!=proof:raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
            stage.mkdir(mode=0o700)
            shutil.copytree(mounted, stage/'Lulu.app', symlinks=True)
            staged_manifest = stage/MANIFEST_NAME
            with staged_manifest.open('xb') as stream:
                stream.write(manifest_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            staged_manifest.chmod(0o600)
            if verify_component_manifest(stage/'Lulu.app', staged_manifest) != proof:
                raise RuntimeError('PACKAGE_STAGE_MISMATCH')
            if delivery_app_tree_identity(stage/'Lulu.app') != identity:
                raise RuntimeError('PACKAGE_STAGE_MISMATCH')
            # Reject a swapped or edited DMG even after the native verification.
            after_digest, after_metadata = _read(dmg, digest_only=True)
            if after_digest != digest or _stamp(after_metadata)[:-1] != _stamp(metadata)[:-1]:
                raise RuntimeError('PACKAGE_FILE_IDENTITY_DRIFT')
        finally:
            detached=_run(['/usr/bin/hdiutil','detach',device or str(mount)])
            if detached.returncode or mount.is_mount():raise RuntimeError('PACKAGE_DETACH_FAILED')
    receipt={'schema_id':DMG_SCHEMA,'verified':True,'release_dmg_sha256':digest,
        'release_dmg_size':metadata.st_size,'release_dmg_identity':{'path':str(dmg),'device':metadata.st_dev,
            'inode':metadata.st_ino,'mode':f'{stat.S_IMODE(metadata.st_mode):04o}','owner':metadata.st_uid,
            'size':metadata.st_size,'mtime_ns':metadata.st_mtime_ns},
        'expected_app':str(app),'app_component_set_digest':proof['app_component_set_digest'],
        'component_manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),
        'product_version':proof['product_version'],'product_build':proof['product_build'],
        'source_revision':proof['source_revision'],'candidate_generation':9,
        'mounted_readonly':True,'detached':True,'contains_secret_material':False}
    return VerifiedDelivery(receipt,manifest_bytes,stage/"Lulu.app")
