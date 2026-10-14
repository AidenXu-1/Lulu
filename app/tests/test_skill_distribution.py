"""System-only catalog and local migration; isolated state and prohibited network."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from src.core.agent_workspace import skill_manager as manager, skills, tools, context
from src.core.agent_workspace.service import WorkspaceService, validate_request
from src.core.agent_workspace.contracts import COMMANDS


def write_fixture(folder,identity):
    folder.mkdir(parents=True)
    (folder/'SKILL.md').write_text(f'---\nname: {identity}\ndescription: 本地测试技能\n---\n按用户要求使用方法。')
    (folder/'references').mkdir();(folder/'references/rules.md').write_text('保留完整参考资料')
    return folder


def install_fixture(identity):
    with tempfile.TemporaryDirectory() as tmp:
        source=write_fixture(Path(tmp).resolve()/identity,identity)
        token=manager.inspect_source(str(source))['entries'][0]['token']
        return manager.install(token=token)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        p=patch.object(skills,'skill_root',return_value=self.root/'skills');p.start();self.addCleanup(p.stop)
        p=patch('socket.socket.connect',side_effect=AssertionError('skill management must stay offline'));p.start();self.addCleanup(p.stop)

    def legacy(self,identity='human-writing',enabled=False):
        manager.listing()
        folder=write_fixture(self.root/'skills'/identity,identity)
        value=manager.package(folder)
        with manager.locked() as (active,state,registry):
            registry['items'][identity]=dict(origin='curated',status='installed',enabled=enabled,
                version='old-version',source='https://github.com/example/old',installed_digest=value['digest'],
                config={'legacy':'kept'},generation='same-generation')
            manager.atomic(state/'registry.json',registry)
        return folder

    def test_fresh_catalog_has_only_six_system_skills(self):
        rows=manager.listing()['entries']
        self.assertEqual(len(rows),6)
        self.assertTrue(all(r['origin']=='system' and r['installed'] and r['enabled'] for r in rows))
        self.assertEqual(set(p.name for p in (manager.resources()/'packages').iterdir()),set(manager.bundled()))
        for identity in ('find-skills','skill-creator','human-writing','khazix-writer'):
            with self.assertRaisesRegex(ValueError,'重新导入'):manager.install(identity)

    def test_legacy_migration_preserves_files_state_and_is_idempotent(self):
        folder=self.legacy();(folder/'references/rules.md').write_text('用户自己的修改')
        digest=manager.package(folder)['digest']
        row=next(r for r in manager.listing()['entries'] if r['name']=='human-writing')
        self.assertEqual(row['origin'],'user');self.assertEqual(row['title'],'活人感写作')
        self.assertFalse(row['enabled']);self.assertEqual(row['version'],'old-version')
        self.assertEqual(row['config'],{'legacy':'kept'});self.assertEqual(row['generation'],'same-generation')
        self.assertFalse(row['update_available']);self.assertEqual(row['digest'],digest)
        with manager.locked() as (_,state,registry):before=(state/'registry.json').read_bytes()
        manager.listing()
        self.assertEqual((state/'registry.json').read_bytes(),before)
        self.assertEqual(manager.package(folder)['digest'],digest)

    def test_uninstalled_cached_legacy_can_restore_without_original_or_network(self):
        folder=self.legacy(enabled=True)
        with manager.locked() as (_,state,registry):
            saved=registry['items']['human-writing'];token='a'*32
            saved.update(status='uninstalled',enabled=False,restore_token=token,restore_digest=manager.package(folder)['digest'])
            folder.rename(state/('retired-'+token));manager.atomic(state/'registry.json',registry)
        row=next(r for r in manager.listing()['entries'] if r['name']=='human-writing')
        self.assertEqual(row['origin'],'user');self.assertTrue(row['recoverable']);self.assertFalse(row['installed'])
        manager.install('human-writing')
        self.assertEqual(manager.details('human-writing','references/rules.md')['text'],'保留完整参考资料')
        self.assertTrue(next(r for r in manager.listing()['entries'] if r['name']=='human-writing')['enabled'])

    def test_empty_uninstalled_recommendations_disappear_without_deleting_files(self):
        manager.listing()
        with manager.locked() as (_,state,registry):
            registry['items']['human-writing']=dict(origin='curated',status='uninstalled',enabled=False)
            unrelated=state/'keep';unrelated.write_text('保留')
            manager.atomic(state/'registry.json',registry)
        self.assertEqual(len(manager.listing()['entries']),6)
        self.assertEqual(unrelated.read_text(),'保留')

    def test_migration_write_failure_preserves_original_state_and_retries(self):
        folder=self.legacy();original=(self.root/'skill-manager/registry.json').read_bytes();content=manager.package(folder)['digest']
        with patch.object(manager,'atomic',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):manager.listing()
        self.assertEqual((self.root/'skill-manager/registry.json').read_bytes(),original)
        self.assertEqual(manager.package(folder)['digest'],content)
        self.assertEqual(next(r for r in manager.listing()['entries'] if r['name']=='human-writing')['origin'],'user')

    def test_former_curated_names_can_be_imported_from_local_package(self):
        for identity in ('find-skills','skill-creator','human-writing','khazix-writer'):
            install_fixture(identity)
            row=next(r for r in manager.listing()['entries'] if r['name']==identity)
            self.assertEqual(row['origin'],'user');self.assertTrue(row['installed'])
        source=write_fixture(self.root/'reserved','lulu-summary')
        token=manager.inspect_source(str(source))['entries'][0]['token']
        with self.assertRaisesRegex(ValueError,'预装'):manager.install(token=token)

    def test_remote_input_rejected_by_core_service_and_retired_tools(self):
        service=WorkspaceService()
        for url in ('https://github.com/a/b','http://example.com/a.zip','file:///tmp/SKILL.md','ftp://example.com/skill'):
            with self.assertRaisesRegex(ValueError,'仅支持本地'):service.skill_inspect(url)
        self.assertFalse((self.root/'skill-manager').exists())
        self.assertNotIn('skills.search',COMMANDS)
        with self.assertRaises(ValueError):validate_request('skill_search',{'query':'writing'})
        exposed={t['name'] for t in context.tool_schemas(loaded_skills=('find-skills','skill-creator'))}
        self.assertTrue({'search_public_skills','inspect_skill_package'}.isdisjoint(exposed))
        for name,args in [('search_public_skills',{'query':'writing'}),('inspect_skill_package',{'url':'https://github.com/a/b'})]:
            with self.assertRaises(ValueError):tools.execute(dict(name=name,arguments=args),{})

    def test_skill_md_import_preserves_sibling_references(self):
        folder=write_fixture(self.root/'input','local-method')
        token=manager.inspect_source(str(folder/'SKILL.md'))['entries'][0]['token']
        manager.install(token=token)
        self.assertEqual(manager.details('local-method','references/rules.md')['text'],'保留完整参考资料')

    def test_local_rollback_keeps_former_curated_user_owned(self):
        folder=self.legacy(enabled=True)
        with manager.locked() as (_,state,registry):
            token='b'*32;prior=dict(registry['items']['human-writing'])
            shutil.copytree(folder,state/('retired-'+token))
            (folder/'references/rules.md').write_text('更新后的参考')
            registry['items']['human-writing'].update(installed_digest=manager.package(folder)['digest'],
                rollback=dict(token=token,metadata=prior))
            manager.atomic(state/'registry.json',registry)
        manager.update('human-writing',rollback=True)
        row=next(r for r in manager.listing()['entries'] if r['name']=='human-writing')
        self.assertEqual(row['origin'],'user');self.assertTrue(row['enabled'])
        self.assertEqual(manager.details('human-writing','references/rules.md')['text'],'保留完整参考资料')
