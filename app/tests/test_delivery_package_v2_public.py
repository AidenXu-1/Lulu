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

    def test_success_stages_verified_bytes_before_detach(self):
        self.expect_stage=True; proof=self.call()
        self.assertEqual(proof.staged_app,self.stage/'Lulu.app')
        self.assertEqual(proof.manifest_bytes,(self.stage/MANIFEST_NAME).read_bytes())
        self.assertEqual(proof.receipt['release_dmg_sha256'],self.expected)
        self.assertTrue(proof.receipt['detached']); self.assertEqual(self.events[-1],'detach')

    def test_independent_digest_is_required_and_mismatch_has_no_stage(self):
        for value in (None,'','0'*64):
            with self.subTest(value=value),self.assertRaises(Exception):self.call(expected_sha256=value)
            self.assertFalse(self.stage.exists())
        self.assertEqual(self.events,[])

    def test_detach_failure_never_returns_installable_proof(self):
        self.expect_stage=True; self.detach_fail=True
        with self.assertRaisesRegex(RuntimeError,'PACKAGE_DETACH_FAILED'):self.call()

    def test_missing_symlink_duplicate_and_unknown_manifest_rejected(self):
        path=self.source/MANIFEST_NAME; original=path.read_bytes()
        for kind in ('missing','symlink','duplicate_key','duplicate_path','unknown_field','legacy_schema'):
            with self.subTest(kind=kind):
                if path.exists() or path.is_symlink():path.unlink()
                if kind=='symlink':
                    path.symlink_to(self.root/'external.json'); (self.root/'external.json').write_bytes(original)
                elif kind=='duplicate_key':path.write_bytes(b'{"schema_id":"ignored",'+original[1:])
                elif kind!='missing':
                    value=json.loads(original)
                    if kind=='duplicate_path':value['components'].append(value['components'][0])
                    if kind=='unknown_field':value['extra']=True
                    if kind=='legacy_schema':value['schema_id']='lulu.agent-component-manifest/v1'
                    path.write_text(json.dumps(value))
                with self.assertRaises(Exception):self.call()
                self.assertFalse(self.stage.exists()); self.assertEqual(self.events[-1],'detach')

if __name__=='__main__':unittest.main()

class ExternalManifestRequiredComponentsTests(unittest.TestCase):
    def test_signed_manifest_cannot_omit_cli_or_core_even_when_identity_matches(self):
        from src.core.agent_entry.component_manifest import verify_component_manifest, _records_for
        from src.core.agent_entry.delivery_identity import delivery_app_tree_identity
        for executable in ('lulu-agent-cli','lulu-agent-core'):
            with self.subTest(executable=executable),TemporaryDirectory() as tmp:
                root=Path(tmp); app=signed_delivery(root); path=root/MANIFEST_NAME
                manifest=json.loads(path.read_bytes())
                (app/'Contents/MacOS'/executable).unlink()
                subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(app)],check=True,capture_output=True)
                manifest['components']=_records_for(app,path)
                manifest['app_component_set_digest']=delivery_app_tree_identity(app)['sha256']
                path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(RuntimeError,'AGENT_ENTRY_COMPONENT_MISMATCH'):
                    verify_component_manifest(app,path)

    def test_v2_app_cannot_use_legacy_internal_manifest(self):
        from src.core.agent_entry.component_manifest import build_component_manifest
        with TemporaryDirectory() as tmp:
            app=signed_delivery(Path(tmp))
            (app/"Contents/Resources/agent_product_manifest.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError,'AGENT_ENTRY_COMPONENT_MISMATCH'):
                build_component_manifest(app_bundle=app,
                    manifest_path=app/'Contents/Resources/agent_product_manifest.json',
                    product_version='0.3.2',product_build='20260904.1',source_revision=fixtures.SOURCE_REVISION,
                    candidate_generation=9,cli_protocol_min='1.0.0',cli_protocol_max='2.0.0',
                    catalog_relative_path='Contents/Resources/src/agent_cli/capability_catalog.json',
                    capability_closed_set=('doctor','transcribe','version'))
