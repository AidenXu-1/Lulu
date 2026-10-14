"""Skill lifecycle and package safety; isolated directories, no network or credentials."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.core.agent_workspace import skill_manager as manager, skills, tools
from src.core.agent_workspace.service import WorkspaceService, validate_request
from src.core.agent_workspace.contracts import COMMANDS


class SkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(skills, 'skill_root', return_value=self.root / 'skills')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def user_skill(self, identity='my-writing'):
        folder = self.root / ('input-' + identity)
        folder.mkdir()
        (folder / 'SKILL.md').write_text(f'---\nname: {identity}\ndescription: 用户自己的写作方法\n---\n\n保留事实。')
        (folder / 'references').mkdir()
        (folder / 'references/rules.md').write_text('完整参考')
        return folder

    def install_user(self):
        source = self.user_skill()
        inspected = manager.inspect_source(str(source))['entries'][0]
        manager.install(token=inspected['token'])
        return source

    def test_user_uninstall_restore_keeps_full_package_without_original(self):
        import shutil
        original = self.install_user()
        active = self.root / 'skills/my-writing'
        (active/'references/rules.md').write_text('用户修改后的方法')
        manager.uninstall('my-writing')
        self.assertFalse(active.exists())
        row = next(r for r in manager.listing()['entries'] if r['name']=='my-writing')
        self.assertTrue(row['recoverable'])
        self.assertEqual(manager.details('my-writing','references/rules.md')['text'],'用户修改后的方法')
        shutil.rmtree(original)
        manager.uninstall('my-writing')  # Repeated requests retain recovery source.
        manager.install('my-writing')
        self.assertTrue(active.is_dir())
        self.assertEqual(manager.details('my-writing','references/rules.md')['text'],'用户修改后的方法')
        self.assertTrue(next(r for r in manager.listing()['entries'] if r['name']=='my-writing')['enabled'])

    def test_changed_restore_package_rejected(self):
        self.install_user()
        manager.uninstall('my-writing')
        with manager.locked() as (active, state, registry):
            source = manager.restore_source(state,registry['items']['my-writing'])
            (source/'references/rules.md').write_text('被外部修改')
        with self.assertRaisesRegex(ValueError,'已变化'):
            manager.install('my-writing')
        self.assertFalse((self.root/'skills/my-writing').exists())

    def test_six_default_system_packages(self):
        rows = manager.listing()['entries']
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r['installed'] and r['enabled'] and r['origin']=='system' for r in rows))
        self.assertEqual(manager.listing()['issues'], [])

    def test_uninstall_survives_restart_and_can_restore_offline(self):
        manager.listing()
        manager.uninstall('lulu-summary')
        self.assertFalse((self.root / 'skills/lulu-summary').exists())
        rows = {r['name']: r for r in manager.listing()['entries']}
        self.assertEqual(rows['lulu-summary']['status'], 'uninstalled')
        self.assertNotIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])
        manager.install('lulu-summary')
        self.assertTrue((self.root / 'skills/lulu-summary/SKILL.md').is_file())
        self.assertEqual(len(list((self.root / 'skill-manager').glob('retired-*'))), 1)

    def test_disable_invalidates_frozen_turn_and_is_durable(self):
        snapshot = skills.catalog()['entries']
        manager.configure('lulu-summary', enabled=False)
        with self.assertRaisesRegex(ValueError, '停用'):
            skills.load('lulu-summary', snapshot)
        self.assertNotIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])
        manager.configure('lulu-summary', enabled=True)
        self.assertIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])

    def test_reference_changes_invalidate_entire_package(self):
        self.install_user()
        snapshot = skills.catalog()['entries']
        (self.root/'skills/my-writing/references/rules.md').write_text('变化')
        with self.assertRaisesRegex(ValueError, '变化'):
            skills.load('my-writing', snapshot)

    def test_existing_user_folder_not_overwritten_by_bootstrap(self):
        folder = self.root / 'skills/lulu-summary'
        folder.mkdir(parents=True)
        (folder/'SKILL.md').write_text('---\nname: lulu-summary\ndescription: 我的同名技能\n---\n自己的规则')
        row = next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')
        self.assertEqual(row['origin'], 'user')
        self.assertFalse(row['recoverable'])
        self.assertIn('自己的规则', (folder/'SKILL.md').read_text())

    def test_manual_missing_folder_not_reinstalled(self):
        manager.listing()
        (self.root/'skills/lulu-summary').rename(self.root/'saved')
        row = next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')
        self.assertEqual(row['status'],'missing')
        self.assertFalse(row['installed'])

    def test_folder_inspect_install_export_and_reimport(self):
        self.install_user()
        target = self.root/'export.zip'
        manager.export('my-writing', str(target))
        with zipfile.ZipFile(target) as z:
            self.assertIn('my-writing/references/rules.md',z.namelist())
        inspected = manager.inspect_source(str(target))['entries'][0]
        self.assertTrue(inspected['conflict'])
        manager.configure('my-writing', enabled=False)
        self.assertIn('已安装', manager.install(token=inspected['token'])['message'])
        self.assertFalse(next(r for r in manager.listing()['entries'] if r['name']=='my-writing')['enabled'])
        (self.root/'skills/my-writing/references/rules.md').write_text('用户后续修改')
        with self.assertRaisesRegex(ValueError, '同名'):
            manager.install(token=inspected['token'])
        manager.uninstall('my-writing')
        manager.install(token=inspected['token'])
        self.assertEqual(manager.details('my-writing','references/rules.md')['text'],'完整参考')

    def test_preview_is_immutable_and_reserved_names_rejected(self):
        source = self.user_skill('lulu-summary')
        preview = manager.inspect_source(str(source))['entries'][0]
        with self.assertRaisesRegex(ValueError, '预装'):
            manager.install(token=preview['token'])
        source = self.user_skill()
        preview = manager.inspect_source(str(source))['entries'][0]
        (self.root/'skill-manager'/('preview-'+preview['token'])/'SKILL.md').write_text('broken')
        with self.assertRaises(ValueError):
            manager.install(token=preview['token'])

    def test_zip_traversal_and_symlinks_rejected(self):
        path=self.root/'bad.zip'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('../outside','bad')
        with self.assertRaises(ValueError):manager.inspect_source(str(path))
        folder=self.user_skill()
        (folder/'link').symlink_to(self.root)
        with self.assertRaises(ValueError):manager.inspect_source(str(folder))
        self.assertFalse((self.root/'outside').exists())

    def test_detail_does_not_read_outside_package(self):
        for reference in ('../registry.json','/etc/passwd','references/../../../secret'):
            with self.assertRaises(ValueError):manager.details('lulu-summary',reference)

    def test_registry_write_failure_recovers_uninstall_without_resurrection(self):
        manager.listing()
        original=manager.atomic
        def crash(path,value):
            if path.name=='registry.json':raise OSError('simulated crash')
            return original(path,value)
        with patch.object(manager,'atomic',side_effect=crash):
            with self.assertRaises(OSError):manager.uninstall('lulu-summary')
        row=next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')
        self.assertFalse(row['installed'])
        self.assertEqual(row['status'],'uninstalled')

    def test_config_and_creation_are_shared_service_operations(self):
        service=WorkspaceService()
        service.skill_list()
        service.skill_configure('lulu-translate',config={'language':'日语','terms':'AI=人工智能'})
        snapshot=skills.catalog()['entries']
        self.assertIn('日语',skills.load('lulu-translate',snapshot)['skill_content'])
        with self.assertRaises(ValueError):service.skill_configure('lulu-translate',config={'read_only':False})
        result=tools.execute(dict(name='create_skill_draft',arguments=dict(name='my-new-skill',description='创作复盘',instructions='根据用户作品复盘。')),dict(options={}))
        self.assertEqual(result['state'],'completed')
        self.assertFalse((self.root/'skills/my-new-skill').exists())
        token=result['entries'][0]['token']
        self.assertIn(token,[r['token'] for r in service.skill_drafts()['entries']])
        service.skill_install(token=token)
        self.assertIn('my-new-skill',[r['name'] for r in service.skills()['entries']])
        for command,(_,operation,_) in COMMANDS.items():
            if command.startswith('skills.'):
                self.assertTrue(callable(getattr(service,operation)))
        validate_request('skill_install',{'token':token})

    def test_no_ambient_install_or_external_search_claim(self):
        result=tools.execute(dict(name='search_skills',arguments={'query':'摘要'}),{})
        self.assertIn('本机',result['message'])
        self.assertIn('lulu-summary',[e['name'] for e in result['entries']])
        result=tools.execute(dict(name='prepare_skill_install',arguments={}),{})
        self.assertEqual(result['state'],'needs_input')

    def test_old_turn_invalid_after_disable_enable_or_reinstall(self):
        snapshot=skills.catalog()['entries']
        manager.configure('lulu-summary',enabled=False)
        manager.configure('lulu-summary',enabled=True)
        with self.assertRaisesRegex(ValueError,'变化'):skills.load('lulu-summary',snapshot)
        snapshot=skills.catalog()['entries']
        manager.uninstall('lulu-summary');manager.install('lulu-summary')
        with self.assertRaisesRegex(ValueError,'变化'):skills.load('lulu-summary',snapshot)

    def test_pinned_update_and_rollback_preserve_preferences(self):
        import shutil
        import hashlib
        manager.listing()
        mirror=self.root/'new-resources'
        shutil.copytree(manager.resources(),mirror)
        path=mirror/'packages/lulu-summary/SKILL.md'
        path.write_text(path.read_text()+'\n新版方法。')
        rows=json.loads((mirror/'catalog.json').read_text())
        row=next(r for r in rows if r['name']=='lulu-summary')
        row.update(version='2.0.0',digest=manager.package(path.parent)['digest'])
        (mirror/'catalog.json').write_text(json.dumps(rows))
        with patch.object(manager,'resources',return_value=mirror):
            self.assertTrue(next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')['update_available'])
            manager.configure('lulu-summary',enabled=False)
            manager.update('lulu-summary')
            self.assertIn('新版方法',manager.details('lulu-summary')['text'])
            manager.update('lulu-summary',rollback=True)
            self.assertNotIn('新版方法',manager.details('lulu-summary')['text'])
            self.assertFalse(next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')['enabled'])

    def test_failed_install_registry_commit_recovers_complete_package(self):
        manager.listing();manager.uninstall('lulu-summary')
        original=manager.atomic
        def crash(path,value):
            if path.name=='registry.json':raise OSError('simulated crash')
            return original(path,value)
        with patch.object(manager,'atomic',side_effect=crash):
            with self.assertRaises(OSError):manager.install('lulu-summary')
        row=next(r for r in manager.listing()['entries'] if r['name']=='lulu-summary')
        self.assertTrue(row['installed'] and row['enabled'])
        self.assertEqual(row['digest'],manager.bundled()['lulu-summary']['digest'])

    def test_previous_skill_content_not_replayed_as_active_instruction(self):
        result=tools.history_message(dict(payload=dict(results=[dict(skill_content='不得继续执行的旧技能',skill_name='old',state='completed')],calls=[],job_id='old')))
        self.assertNotIn('不得继续执行',result['content'])
        self.assertIn('old',result['content'])

    def test_frozen_macos_resource_link_is_resolved_inside_bundle(self):
        import shutil
        contents=self.root/'Lulu.app/Contents'
        frameworks=contents/'Frameworks';frameworks.mkdir(parents=True)
        resources=contents/'Resources/internal-skills'
        shutil.copytree(manager.resources(),resources)
        (frameworks/'internal-skills').symlink_to('../Resources/internal-skills')
        with patch.object(manager.sys,'frozen',True,create=True), patch.object(manager.sys,'_MEIPASS',str(frameworks),create=True):
            self.assertEqual(manager.resources(),resources)
            self.assertEqual(len(manager.listing()['entries']),6)
