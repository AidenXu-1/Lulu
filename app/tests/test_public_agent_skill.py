"""Public Skill lifecycle and verification boundary, using isolated fake Apps."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]/'agent-skill'

def module(file,name):
    spec=importlib.util.spec_from_file_location(name,file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class PublicSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.manager=module(ROOT/'manage.py','public_skill_manage')
        self.source=ROOT/'lulu-agent';self.directory=self.root/'agent/skills'

    def test_install_and_uninstall_touch_only_optional_skill(self):
        self.directory.mkdir(parents=True)
        neighbor=self.directory/'keep';neighbor.write_text('other skill')
        self.manager.manage('install',self.directory,self.source)
        installed=self.directory/'lulu-agent'
        self.assertEqual(self.manager.manifest(installed),self.manager.manifest(self.source))
        self.manager.manage('uninstall',self.directory)
        self.assertFalse(installed.exists());self.assertEqual(neighbor.read_text(),'other skill')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),['keep'])

    def test_existing_modified_unmanaged_and_symlink_files_are_protected(self):
        self.manager.manage('install',self.directory,self.source)
        with self.assertRaisesRegex(ValueError,'已有同名'):self.manager.manage('install',self.directory,self.source)
        (self.directory/'lulu-agent/SKILL.md').write_text('my edit')
        with self.assertRaisesRegex(ValueError,'修改'):self.manager.manage('uninstall',self.directory)
        (self.directory/'lulu-agent'/self.manager.RECEIPT).unlink()
        with self.assertRaisesRegex(ValueError,'不由本安装器'):self.manager.manage('uninstall',self.directory)
        link=self.root/'symlink';link.symlink_to(self.directory)
        with self.assertRaises(ValueError):self.manager.manage('uninstall',link)

    def test_legacy_managed_path_never_mutated(self):
        with patch.object(self.manager.Path,'home',return_value=self.root):
            for action in ['install','uninstall']:
                with self.assertRaisesRegex(ValueError,'受管位置'):
                    self.manager.manage(action,self.root/'.agents/skills',self.source)
        self.assertFalse((self.root/'.agents').exists())

    def test_public_verifier_requires_product_not_optional_launcher_or_skill(self):
        verifier=module(self.source/'scripts/verify_lulu.py','public_verifier')
        app=self.root/'Lulu.app';marker=app/'Contents/Resources/component_delivery.v2.json'
        marker.parent.mkdir(parents=True);marker.write_text('{}')
        manifest=dict(app_component_set_digest='a'*64,product_version='1.0.0',product_build='fixture',cli_protocol={'min':'1.0.0'})
        with patch.object(verifier,'APP',app),patch.object(verifier,'_verify_v2',return_value=(manifest,b'{}')) as checked:
            result=verifier.verify(app=app,state_root=self.root/'state')
            self.assertTrue(result['verified']);checked.assert_called_once()
            self.assertEqual(result['canonical_cli'],str(app/'Contents/MacOS/lulu-agent-cli'))
        with patch.object(verifier,'APP',app),patch.object(verifier,'_verify_v2',side_effect=verifier.VerificationError('damaged')):
            with self.assertRaisesRegex(verifier.VerificationError,'damaged'):verifier.verify(app=app)
        with self.assertRaises(verifier.VerificationError):verifier.verify(app=self.root/'other.app')

    def test_public_instructions_have_no_optional_launcher_dependency_or_missing_links(self):
        import re
        self.assertIn('capabilities --output json',(self.source/'SKILL.md').read_text())
        for p in self.source.rglob('*.md'):
            for link in re.findall(r'\]\(([^)]+)\)',p.read_text()):
                if '://' not in link:self.assertTrue((p.parent/link).is_file(),(p,link))
            self.assertNotIn('~/.local/bin/lulu ',p.read_text())
        self.assertFalse((self.source/'manifest.json').exists())
