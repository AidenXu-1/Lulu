"""Native signed payloads; simulated read-only disk command seam, no installed state."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import test_generation9_formal_install_public as fixtures
from src.core.agent_entry.component_manifest import build_component_manifest
from src.core.agent_entry.delivery_identity import prepare_delivery_marker, prepare_link_policy, MANIFEST_NAME
from src.core.agent_entry.delivery_package import verify_delivery_dmg


def signed_delivery(root):
    app = fixtures._fixture_app(root)
    (app/'Contents/Resources/agent_product_manifest.json').unlink()
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'Lulu',
        'CFBundleIdentifier':'com.lulu.test.fixture','CFBundlePackageType':'APPL'}))
    for name in ('Lulu','lulu-agent-cli','lulu-agent-core'):
        shutil.copyfile('/usr/bin/true',app/'Contents/MacOS'/name)
        (app/'Contents/MacOS'/name).chmod(0o755)
    prepare_delivery_marker(app); prepare_link_policy(app)
    subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(app)],check=True,capture_output=True)
    manifest=root/MANIFEST_NAME
    value=build_component_manifest(app_bundle=app,manifest_path=manifest,
        product_version='0.3.2',product_build='20260904.1',source_revision=fixtures.SOURCE_REVISION,
        candidate_generation=9,cli_protocol_min='1.0.0',cli_protocol_max='2.0.0',
        catalog_relative_path='Contents/Resources/src/agent_cli/capability_catalog.json',
        capability_closed_set=('doctor','transcribe','version'))
    manifest.write_text(json.dumps(value))
    installer=root/'Install Lulu.app';binpath=installer/'Contents/MacOS/InstallLulu';binpath.parent.mkdir(parents=True)
    shutil.copyfile('/usr/bin/true',binpath);binpath.chmod(0o755)
    (installer/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'InstallLulu',
        'CFBundleIdentifier':'com.lulu.transcribe.installer','CFBundlePackageType':'APPL'}))
    subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(installer)],check=True,capture_output=True)
    return app


class DeliveryPackageV2PublicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.source=self.root/'source'
        self.app=signed_delivery(self.source)
        self.dmg=self.root/'Lulu.dmg'; self.dmg.write_bytes(b'exact disk fixture')
        self.expected=hashlib.sha256(self.dmg.read_bytes()).hexdigest()
        self.stage=self.root/'stage'; self.events=[]; self.detach_fail=False; self.expect_stage=False
        self.patcher=patch('src.core.agent_entry.delivery_package._run',side_effect=self.command)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def command(self,args,timeout=120):
        if args[0]=='/sbin/mount':
            return subprocess.CompletedProcess(args,0,f'/dev/disk99 on {self.mount.resolve()} (hfs, local, read-only)'.encode())
        action=args[1]; self.events.append(action)
        if action=='attach':
            self.mount=Path(args[args.index('-mountpoint')+1])
            for item in self.source.iterdir():
                target=self.mount/item.name
                if item.is_symlink():target.symlink_to(item.readlink())
                elif item.is_dir():shutil.copytree(item,target,symlinks=True)
                else:shutil.copy2(item,target)
            return subprocess.CompletedProcess(args,0,plistlib.dumps({'system-entities':[
                {'mount-point':str(self.mount),'dev-entry':'/dev/disk99'}]}))
        if action=='detach':
            if self.expect_stage:self.assertTrue((self.stage/'Lulu.app').is_dir())
            return subprocess.CompletedProcess(args,int(self.detach_fail),b'')
        return subprocess.CompletedProcess(args,0,b'')

    def call(self,**kwargs):
        return verify_delivery_dmg(self.dmg,expected_app=self.app,
            expected_sha256=kwargs.pop('expected_sha256',self.expected),stage_root=self.stage,**kwargs)


if __name__=='__main__':unittest.main()
