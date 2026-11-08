"""Development delivery cannot silently write into the stable installation."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src import edition
from src.core import secrets

APP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('edition_profile_gate', APP/'packaging/profile_gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


class DevelopmentEditionTests(unittest.TestCase):
    def tearDown(self):
        edition.is_experience.cache_clear()
        secrets._cache.clear(); secrets._memory.clear()

    def test_sealed_profile_has_distinct_name_id_and_no_upload(self):
        args=dict(version='4.3.3',build='20261107.13',source_commit='a'*40,source_tree='b'*40)
        stable=gate.profile_payload('external_candidate',**args)
        dev=gate.profile_payload('external_candidate',edition=edition.EXPERIENCE,**args)
        self.assertEqual(stable['app_name'],'Lulu');self.assertNotIn('edition',stable)
        self.assertEqual(dev['app_name'],'Lulu 开发体验版')
        self.assertNotEqual(stable['bundle_id'],dev['bundle_id'])
        self.assertEqual(dev['upload_target'],'none')
        self.assertTrue(edition.profile_is_experience(dev))
        with self.assertRaises(RuntimeError):edition.profile_is_experience(dict(dev,bundle_id=stable['bundle_id']))
        with self.assertRaises(ValueError):gate.profile_payload('trusted_test',edition=edition.EXPERIENCE,**args)

    def test_environment_cannot_select_development_storage(self):
        with patch.object(edition.sys,'frozen',False,create=True),patch.dict(os.environ,{'LULU_EDITION':edition.EXPERIENCE}),patch.object(Path,'home',return_value=Path('/local/user')),patch.object(edition.platform,'system',return_value='Darwin'):
            self.assertEqual(edition.product_data_root(),Path('/local/user/Library/Application Support/Lulu'))
            self.assertEqual(edition.config_root(),Path('/local/user/.本地转录app'))

    def test_isolated_storage_and_optional_commands_do_not_overlap(self):
        with patch.object(edition,'is_experience',return_value=True),patch.object(Path,'home',return_value=Path('/local/user')),patch.object(edition.platform,'system',return_value='Darwin'):
            root=edition.product_data_root()
            self.assertEqual(root,Path('/local/user/Library/Application Support/Lulu Development Experience'))
            self.assertEqual(edition.config_root(),root/'preferences')
            self.assertEqual(edition.optional_integration_targets(),(Path('/local/user/.agents/skills/lulu-experience-agent'),Path('/local/user/.local/bin/lulu-experience')))
            from src.core import models_manager
            from src.core.updater.constants import default_updates_root
            self.assertEqual(models_manager._app_data_dir(),root)
            self.assertEqual(default_updates_root(),root/'updates')

    def test_real_workspace_writes_leave_stable_preferences_unchanged(self):
        from src.core import library
        from src.core.agent_workspace.store import WorkspaceStore
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d).resolve()),patch.object(edition.platform,'system',return_value='Darwin'):
            with patch.object(edition,'is_experience',return_value=False):
                stable_root=edition.product_data_root()/'library'
            with patch.object(library,'LIBRARY_DIR',stable_root):
                stable=WorkspaceStore();stable.preference('edition-proof',{'value':'stable'})
                stable_bytes=stable.path.read_bytes()
            with patch.object(edition,'is_experience',return_value=True),patch.object(library,'LIBRARY_DIR',edition.product_data_root()/'library'):
                dev=WorkspaceStore();dev.preference('edition-proof',{'value':'development'})
                self.assertNotEqual(dev.path,stable.path)
                self.assertEqual(dev.preference('edition-proof'),{'value':'development'})
            self.assertEqual(stable.path.read_bytes(),stable_bytes)
            with patch.object(library,'LIBRARY_DIR',stable_root):
                self.assertEqual(stable.preference('edition-proof'),{'value':'stable'})

    def test_core_layout_uses_same_edition_root_as_install_handover(self):
        from src.core.storage_layout import SharedProductStorageLayout
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d).resolve()),patch.object(edition.platform,'system',return_value='Darwin'):
            support=Path(d).resolve()/'Library/Application Support'
            support.mkdir(parents=True,mode=0o700)
            for development in (False,True):
                with patch.object(edition,'is_experience',return_value=development):
                    root=edition.product_data_root()
                    layout=SharedProductStorageLayout.from_application_support_root(support)
                    self.assertEqual(layout.product_root,root)
                    self.assertEqual(layout.jobs,root/'jobs')
                    self.assertEqual(layout.library,root/'library')
                    self.assertEqual(layout.models,root/'models')

    def test_development_basename_requires_matching_sealed_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            app=Path(d)/'Lulu 开发体验版.app';res=app/'Contents/Resources';res.mkdir(parents=True)
            self.assertFalse(edition.valid_app_basename(app))
            payload={'profile':'external_candidate','edition':edition.EXPERIENCE,**edition.EXPERIENCE_META}
            (res/'build_profile.json').write_text(json.dumps(payload))
            self.assertTrue(edition.valid_app_basename(app))
            payload['bundle_id']='com.lulu.transcribe';(res/'build_profile.json').write_text(json.dumps(payload))
            self.assertFalse(edition.valid_app_basename(app))

    def test_development_install_target_does_not_accept_stable_app(self):
        from src.core.agent_entry import standalone_paths
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d).resolve()),patch.object(edition,'is_experience',return_value=True):
            app=Path(d).resolve()/'Applications/Lulu 开发体验版.app';res=app/'Contents/Resources';res.mkdir(parents=True)
            (res/'build_profile.json').write_text(json.dumps({'profile':'external_candidate','edition':edition.EXPERIENCE,**edition.EXPERIENCE_META}))
            (app/standalone_paths.DESCRIPTOR).write_text('{}')
            self.assertEqual(standalone_paths.installed_app(app),app)
            with self.assertRaises(RuntimeError):standalone_paths.installed_app(Path('/Applications/Lulu.app'))

    def test_model_credentials_reuse_is_read_only_for_stable_service(self):
        records={('Lulu','workspace-key-example'):'original-key',('Lulu','feishu_wiki'):'original-feishu'}
        backend=Mock();backend.get_password.side_effect=lambda service,key:records.get((service,key))
        backend.set_password.side_effect=lambda service,key,value:records.__setitem__((service,key),value)
        with patch.object(secrets,'is_experience',return_value=True),patch.object(secrets,'_backend',return_value=backend):
            self.assertEqual(secrets.get_key('workspace-key-example'),'original-key')
            secrets.set_key_verified('workspace-key-example','experience-key')
            self.assertEqual(records[('Lulu','workspace-key-example')],'original-key')
            self.assertEqual(secrets.get_key('workspace-key-example'),'experience-key')
            self.assertIsNone(secrets.get_key('feishu_wiki'))
            self.assertEqual(records[('Lulu','feishu_wiki')],'original-feishu')
        backend.delete_password.assert_not_called()

    def test_clearing_experience_connection_never_revives_or_deletes_stable_key(self):
        records={('Lulu','dashscope'):'stable-key'}
        backend=Mock();backend.get_password.side_effect=lambda service,key:records.get((service,key))
        backend.set_password.side_effect=lambda service,key,value:records.__setitem__((service,key),value)
        with patch.object(secrets,'is_experience',return_value=True),patch.object(secrets,'_backend',return_value=backend):
            self.assertEqual(secrets.get_key('dashscope'),'stable-key')
            secrets.set_key_verified('dashscope','')
            secrets._cache.clear()
            self.assertIsNone(secrets.get_key('dashscope',refresh=True))
            self.assertEqual(records[('Lulu','dashscope')],'stable-key')
        backend.delete_password.assert_not_called()

    def test_experience_never_downloads_or_stages_stable_update(self):
        from src.core.updater.service import UpdateService
        from src.core.updater.models import UpdateError
        with tempfile.TemporaryDirectory() as d,patch.object(edition,'is_experience',return_value=True):
            client=Mock();svc=UpdateService(root=Path(d),client=client)
            result=svc.check(manual=True)
            self.assertEqual(result.error_code,'development_edition')
            self.assertFalse(result.update_available)
            with self.assertRaises(UpdateError):svc.download()
            with self.assertRaises(UpdateError):svc.stage(Path(d)/'unknown.zip')
            self.assertFalse(client.mock_calls)


if __name__=='__main__':unittest.main()
