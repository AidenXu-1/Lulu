from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class DeliveryIdentityV2PublicTests(unittest.TestCase):
    def test_closed_internal_link_and_signature_bytes_are_in_final_identity(self):
        from src.core.agent_entry.delivery_identity import prepare_link_policy, delivery_app_tree_identity
        with TemporaryDirectory() as tmp:
            app=Path(tmp)/'Lulu.app'
            resources=app/'Contents/Resources';resources.mkdir(parents=True)
            (resources/'value').write_bytes(b'known payload')
            (resources/'alias').symlink_to('value')
            signature=app/'Contents/_CodeSignature/CodeResources';signature.parent.mkdir()
            signature.write_bytes(b'sealed bytes')
            prepare_link_policy(app)
            before=delivery_app_tree_identity(app)
            self.assertEqual(before['schema_id'],'lulu.canonical-app-tree/v2')
            self.assertEqual(before['symlink_count'],1)
            self.assertEqual(before['excluded_paths'],[])
            signature.write_bytes(b'changed seal')
            self.assertNotEqual(delivery_app_tree_identity(app)['sha256'],before['sha256'])

    def test_unknown_absolute_escape_cycle_and_retargeted_links_are_rejected(self):
        from src.core.agent_entry.delivery_identity import prepare_link_policy, delivery_app_tree_identity
        for change in ('extra', 'absolute', 'escape', 'cycle', 'retarget'):
            with self.subTest(change=change), TemporaryDirectory() as tmp:
                app=Path(tmp)/'Lulu.app'
                resources=app/'Contents/Resources';resources.mkdir(parents=True)
                (resources/'one').write_bytes(b'one')
                (resources/'two').write_bytes(b'two')
                alias=resources/'alias';alias.symlink_to('one')
                prepare_link_policy(app)
                if change=='extra': (resources/'extra').symlink_to('one')
                else:
                    alias.unlink()
                    target={'absolute':str(resources/'one'),'escape':'../../../outside',
                            'cycle':'alias','retarget':'two'}[change]
                    (Path(tmp)/'outside').write_bytes(b'outside')
                    alias.symlink_to(target)
                with self.assertRaises(Exception):delivery_app_tree_identity(app)

    def test_target_mode_type_and_contents_change_complete_identity(self):
        from src.core.agent_entry.delivery_identity import prepare_link_policy, delivery_app_tree_identity
        for change in ('mode','type','contents'):
            with self.subTest(change=change),TemporaryDirectory() as tmp:
                app=Path(tmp)/'Lulu.app';resources=app/'Contents/Resources';resources.mkdir(parents=True)
                target=resources/'one';target.write_bytes(b'one');(resources/'alias').symlink_to('one')
                prepare_link_policy(app);before=delivery_app_tree_identity(app)
                if change=='mode':target.chmod(0o700)
                elif change=='contents':target.write_bytes(b'changed')
                else:
                    target.unlink();target.mkdir()
                    with self.assertRaises(Exception):delivery_app_tree_identity(app)
                    continue
                self.assertNotEqual(delivery_app_tree_identity(app)['sha256'],before['sha256'])

    def test_external_manifest_covers_final_signatures_without_modifying_app(self):
        from src.core.agent_entry.component_manifest import build_component_manifest, verify_component_manifest
        from src.core.agent_entry.delivery_identity import prepare_link_policy, delivery_app_tree_identity
        import test_generation9_formal_install_public as fixtures
        import json, shutil, plistlib, subprocess
        with TemporaryDirectory() as tmp:
            root=Path(tmp);app=fixtures._fixture_app(root/'candidate')
            (app/'Contents/Resources/agent_product_manifest.json').unlink()
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'Lulu',
                'CFBundleIdentifier':'com.lulu.test.fixture','CFBundlePackageType':'APPL'}))
            for name in ('Lulu','lulu-agent-cli','lulu-agent-core'):
                shutil.copyfile('/usr/bin/true',app/'Contents/MacOS'/name)
                (app/'Contents/MacOS'/name).chmod(0o755)
            from src.core.agent_entry.delivery_identity import prepare_delivery_marker
            prepare_delivery_marker(app)
            prepare_link_policy(app)
            subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(app)],check=True,capture_output=True)
            before=delivery_app_tree_identity(app)
            path=root/'Lulu.components.v2.json'
            manifest=build_component_manifest(app_bundle=app,manifest_path=path,
                product_version='0.3.2',product_build='20260904.1',source_revision=fixtures.SOURCE_REVISION,
                candidate_generation=9,cli_protocol_min='1.0.0',cli_protocol_max='2.0.0',
                catalog_relative_path='Contents/Resources/src/agent_cli/capability_catalog.json',
                capability_closed_set=('doctor','transcribe','version'))
            self.assertEqual(manifest['schema_id'],'lulu.agent-component-manifest/v2')
            path.write_text(json.dumps(manifest))
            self.assertEqual(verify_component_manifest(app,path)['state'],'ready')
            self.assertEqual(delivery_app_tree_identity(app),before)
            self.assertIn('Contents/_CodeSignature/CodeResources',[r['path'] for r in manifest['components']])

if __name__=='__main__':unittest.main()
