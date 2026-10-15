"""Standard DMG: the exact signed update App and an Applications alias."""
from pathlib import Path
import json
import os
import plistlib
import shutil
import subprocess
import tempfile

from src.core.agent_entry.standalone_source import inspect_install_source
from src.core.agent_entry.delivery_package import _read, _run


def prepare_payload(app, destination):
    app, destination = Path(app), Path(destination)
    source = inspect_install_source(app)
    destination.mkdir()
    shutil.copytree(app, destination/'Lulu.app', symlinks=True)
    os.symlink('/Applications', destination/'Applications')
    if inspect_install_source(destination/'Lulu.app') != source:
        raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
    return source


def verify_drag_dmg(dmg, *, expected_app):
    source = inspect_install_source(Path(expected_app))
    digest, _ = _read(dmg, digest_only=True)
    if _run(['/usr/bin/hdiutil', 'verify', str(dmg)], 300).returncode:
        raise RuntimeError('PACKAGE_HASH_MISMATCH')
    with tempfile.TemporaryDirectory(prefix='lulu-drag-verify-') as temporary:
        mount = Path(temporary).resolve()
        result = _run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-plist',
                       '-mountpoint', str(mount), str(dmg)])
        if result.returncode:
            raise RuntimeError('PACKAGE_MOUNT_FAILED')
        device = None
        try:
            entities = plistlib.loads(result.stdout).get('system-entities', [])
            matching = [entry for entry in entities if entry.get('mount-point') == str(mount)]
            if len(matching) != 1:
                raise RuntimeError('PACKAGE_MOUNT_FAILED')
            device = matching[0]['dev-entry']
            mounted = _run(['/sbin/mount'])
            marker = f' on {mount} ('.encode()
            if mounted.returncode or not any(marker in line and b'read-only' in line for line in mounted.stdout.splitlines()):
                raise RuntimeError('PACKAGE_MOUNT_NOT_READONLY')
            if ({p.name for p in mount.iterdir()} - {'.DS_Store', '.Trashes', '.fseventsd'} != {'Lulu.app', 'Applications'}
                    or not (mount/'Applications').is_symlink()
                    or os.readlink(mount/'Applications') != '/Applications'
                    or inspect_install_source(mount/'Lulu.app') != source):
                raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
            if _read(dmg, digest_only=True)[0] != digest:
                raise RuntimeError('PACKAGE_FILE_IDENTITY_DRIFT')
        finally:
            if _run(['/usr/bin/hdiutil', 'detach', device or str(mount)]).returncode or mount.is_mount():
                raise RuntimeError('PACKAGE_DETACH_FAILED')
    return {'schema_id': 'lulu.drag-delivery/v1', 'verified': True, 'detached': True,
        'mounted_readonly': True, 'release_dmg_sha256': digest,
        'app_component_set_digest': source.receipt['app_component_set_digest']}


def create_delivery(app, output):
    app, output = Path(app).resolve(), Path(output).resolve()
    if output.exists():
        raise RuntimeError('DELIVERY_OUTPUT_MUST_BE_NEW')
    work = output.parent.parent/('.'+output.stem+'-delivery-evidence')
    work.mkdir()
    prepare_payload(app, work/'payload')
    subprocess.run(['/usr/bin/hdiutil', 'create', '-srcfolder', str(work/'payload'),
        '-volname', 'Lulu', '-fs', 'HFS+', '-format', 'UDZO', str(output)], check=True)
    receipt = verify_drag_dmg(output, expected_app=app)
    (work/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    # Hashes remain release evidence; users only need the DMG itself.
    (work/(output.name+'.sha256')).write_text(receipt['release_dmg_sha256']+'  '+output.name+'\n')
    return receipt


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(create_delivery(arguments.app, arguments.output), indent=2))
