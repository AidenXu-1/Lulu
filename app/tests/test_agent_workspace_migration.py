import json
import unittest
import hashlib
from unittest.mock import patch
from src.config import AppConfig
from src.core import library
from src.core.agent_workspace.migration import migrate
from src.core.agent_workspace.store import workspace_root
from src.core.agent_workspace.service import WorkspaceService
from src.core.library_query import _timestamp
from tests import test_agent_workspace


class WorkspaceMigrationTests(unittest.TestCase):
    setUp=test_agent_workspace.AgentWorkspaceTests.setUp


    def test_mixed_legacy_library_keeps_files_links_and_presets_after_interrupted_upgrade(self):
        folder = library.create_folder('rewrite', '旧作品')
        rewritten = library.save_rewrite('改写正文', '旧改写', source_id=self.eid, folder_id=folder)
        audio = self.root / '已有声音.wav'
        audio.write_bytes(b'keep existing audio bytes')
        voice = library.save_voice(title='已有声音', audio_path=audio, text='生成时的文字',
            engine='IndexTTS 2.5', voice_name='已有音色', config_summary='原设置',
            source_id=rewritten, meta={'model_version': '2.5'})
        config = AppConfig()
        config.copywriting_user_templates = {'rewrite': [dict(id='t', name='保留模板', text='原模板')]}
        config.copywriting_prompts = {'polish': {'custom_text': '原有提示词'}}
        originals = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        store = self.service.store
        original_write = library._write_atomic_text
        def interrupted(path, text):
            if path.name == 'migration-v1.json' and json.loads(text)['phase'] == 'complete':
                raise OSError('interrupted after presets were imported')
            return original_write(path, text)
        with patch.object(library, '_write_atomic_text', side_effect=interrupted):
            with self.assertRaises(OSError):
                migrate(store, config)
        reopened = WorkspaceService()
        result = migrate(reopened.store, config)
        records = {e['old_id']: e for e in result['entries']}
        self.assertEqual(records[rewritten]['source_id'], self.eid)
        self.assertEqual(records[voice]['source_id'], rewritten)
        self.assertEqual(records[rewritten]['folder_id'], folder)
        self.assertEqual(len(reopened.presets()['custom']), 2)
        self.assertEqual(migrate(WorkspaceService().store, config), result)
        self.assertEqual(set(e['id'] for e in reopened.list()['entries']), {self.eid, rewritten})
        self.assertEqual(reopened.audio(voice)['source_text_snapshot'], '生成时的文字')
        # Returning to old readers needs no reverse move, rename or data rewrite.
        self.assertEqual(library.load(rewritten).text, '改写正文')
        self.assertEqual(library.load_voice(voice)['source_id'], rewritten)
        for path, before in originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(before).digest(), path)
