"""Slow readiness and native dialog regressions; no network or user credentials."""
import time
import threading
from unittest.mock import patch
from PySide6.QtCore import QTimer, Qt
from tests.model_settings_fixtures import ControllerCase, m
from tests.test_voice_refresh_stability import VoiceRefreshStabilityTests
from tests.test_feishu_storage_ui import TestFeishuActionDialog


class DefaultSelectionTests(ControllerCase):
    def setUp(self):
        super().setUp()
        self.c.state = dict(localModels=dict(extract=[dict(id='whisper', ready=True), dict(id='sensevoice', ready=True)],
                                            voice=[dict(id=k, ready=True) for k in m.MODEL_IDS if k not in {'whisper', 'sensevoice', 'qwen_asr'}]))

    def test_selection_saves_immediately_without_model_recheck(self):
        replies=[]
        self.c.reply.connect(lambda rid,result: replies.append(result))
        with patch.object(self.c, '_selection_ready', side_effect=AssertionError('Selection must not scan models')):
            self.c.selection_request('select', dict(id='whisper', action='default'))
        self.assertTrue(replies[0]['ok'])
        self.assertEqual(self.cfg.engine_name, 'local_whisper_mlx')
        self.assertEqual(self.c.owned_workers(), [])
        self.cfg.save.assert_called_once()

    def test_voice_defaults_also_save_without_loading_or_rechecking(self):
        with patch.object(self.c, '_selection_ready', side_effect=AssertionError('Selection must not scan models')):
            for key in ('indextts2', 'indextts25', 'voxcpm2', 'qwen_tts'):
                self.c.selection_request(key, dict(id=key, action='default'))
                self.assertEqual(self.cfg.default_tts_model, m.MODEL_IDS[key])
        self.assertEqual(self.c.owned_workers(), [])

    def test_failed_save_keeps_original_and_reports_failure(self):
        self.cfg.save.return_value=False;replies=[]
        self.c.reply.connect(lambda rid,result: replies.append(result))
        self.c.selection_request('select', dict(id='whisper', action='default'))
        self.assertFalse(replies[0]['ok'])
        self.assertEqual(self.cfg.engine_name, 'local_sensevoice')
        self.changed.assert_not_called()

    def test_unready_or_unknown_snapshot_cannot_become_default(self):
        replies=[]
        self.c.reply.connect(lambda rid,result: replies.append(result))
        for state in (dict(localModels=dict(extract=[dict(id='whisper', ready=False)])), {}):
            self.c.state = state
            self.c.selection_request('select', dict(id='whisper', action='default'))
            self.assertFalse(replies[-1]['ok'])
        self.cfg.save.assert_not_called()
        self.assertEqual(self.cfg.engine_name, 'local_sensevoice')

    def test_relocation_still_blocks_preference_write(self):
        replies=[]
        self.c.reply.connect(lambda rid,result: replies.append(result))
        self.c.relocation_busy=lambda:True
        self.c.selection_request('select', dict(id='whisper', action='default'))
        self.assertFalse(replies[0]['ok'])
        self.cfg.save.assert_not_called()

    def test_old_snapshot_cannot_revert_saved_default(self):
        self.c.state = dict(localModels=dict(extract=[dict(id='whisper', default=True), dict(id='sensevoice', default=False)]))
        self.c.publish()
        self.assertEqual([row['default'] for row in self.c.state['localModels']['extract']], [False, True])


class VoiceStatusTests(VoiceRefreshStabilityTests):
    def test_invalidated_read_cannot_restore_stale_ready_status(self):
        from src.core.synthesis import runtime_packages
        for worker in list(self.page._model_status_reads.values()):
            worker.wait()
        self.app.processEvents()
        self.page.invalidate_model_status()
        with patch.object(runtime_packages, 'status', return_value='ready'):
            self.page.model_readiness('voxcpm2')
            self.page._model_status_reads['voxcpm2'].wait(1000)
            self.page.invalidate_model_status()
            self.app.processEvents()
        self.assertNotIn('voxcpm2', self.page._model_status_cache)
        with patch.object(runtime_packages, 'status', return_value='runtime_only'):
            self.page.model_readiness('voxcpm2')
            self.page._model_status_reads['voxcpm2'].wait(1000)
            self.app.processEvents()
        self.assertEqual(self.page.model_readiness('voxcpm2'), (False, 'runtime_only'))

    def test_refresh_does_not_verify_runtime_on_ui_thread(self):
        from src.core.synthesis import qwen3_runtime,runtime_packages
        ui=threading.get_ident();calls=[]
        def slow(*args):calls.append(threading.get_ident());time.sleep(.08);return False,'未安装'
        with patch.object(qwen3_runtime,'readiness',side_effect=slow),patch.object(runtime_packages,'status',return_value='not_installed'):
            for worker in self.page._model_status_reads.values():worker.wait()
            self.app.processEvents()
            self.page.invalidate_model_status()
            self.page.refresh()
            deadline=time.monotonic()+2
            while not calls and time.monotonic()<deadline:
                self.app.processEvents();time.sleep(.005)
            self.assertTrue(calls)
            self.assertNotIn(ui,calls)
            self.page.shutdown_workers()


class FeishuVisibilityTests(TestFeishuActionDialog):
    def test_titlebar_preparation_never_hides_an_executing_dialog(self):
        from src.ui import feishu_dialog
        seen=[]
        def native_flags(window):
            seen.append(window.isVisible())
            window.setWindowFlags(window.windowFlags() | Qt.ExpandedClientAreaHint | Qt.NoTitleBarBackgroundHint)
        with patch.object(feishu_dialog,'_unify_feishu_titlebar',side_effect=native_flags):
            dialog=self._dialog()
            QTimer.singleShot(100,dialog.reject)
            t=time.monotonic();dialog.exec();elapsed=time.monotonic()-t
        self.assertFalse(any(seen),'Changing flags after show hides the modal')
        self.assertGreater(elapsed,.075)

from tests.test_library_workspace_core import WorkspaceCoreTests

class FolderDeletionTests(WorkspaceCoreTests):
    def test_workspace_delete_folder_preserves_members_and_other_folders(self):
        from src.core import library
        from src.core.agent_workspace.service import handle
        f=handle('create_folder',{'name':'资料','kind':'documents'})['id']
        other=handle('create_folder',{'name':'保留','kind':'documents'})['id']
        eid=library.save_document('保留正文','文稿',folder_id=f)
        self.assertEqual(library._load_raw(eid)['folder_id'], f)
        result=handle('delete_folder',{'folder_id':f})
        self.assertTrue(result['materials_preserved'])
        self.assertIsNone(library._load_raw(eid)['folder_id'])
        self.assertEqual(library.load_markdown(eid),'保留正文')
        self.assertIn(other,[v['id'] for v in library.list_folders('transcript')])
        with self.assertRaises(FileNotFoundError):handle('delete_folder',{'folder_id':f})

    def test_audio_folder_deletion_preserves_audio_bytes(self):
        from src.core import library
        from src.core.agent_workspace.service import handle
        f=handle('create_folder',{'name':'声音','kind':'voice'})['id']
        audio=self.root/'keep.wav';audio.write_bytes(b'keep audio')
        eid=library.save_voice(title='音频',audio_path=audio,text='声音',engine='test',voice_name='test',config_summary='test',folder_id=f)
        handle('delete_folder',{'folder_id':f})
        self.assertEqual(audio.read_bytes(),b'keep audio')
        self.assertIsNone(library._load_raw(eid)['folder_id'])
