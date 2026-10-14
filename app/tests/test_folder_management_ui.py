"""Native WebEngine library actions use isolated local materials."""
from tests.test_agent_workspace_ui import AgentWorkspaceUiTests
from src.core import library

class FolderManagementUiTests(AgentWorkspaceUiTests):
    def test_delete_folder_cancel_then_confirm_preserves_open_document(self):
        folder=library.create_folder('transcript','待整理')
        library.set_entry_folder(self.eid,folder)
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.widget.refresh()
        self.until(lambda:self.js('window.lulu.state.folders.length')==1)
        self.js("document.querySelector('#list-menu').click()")
        self.until(lambda:self.js('Boolean(document.querySelector(\'#manage-folders\'))'))
        self.js('document.querySelector(\'#manage-folders\').click()')
        self.js('document.querySelector("[data-delete-folder]").click()')
        self.assertTrue(self.js('document.querySelector("#dialog").textContent.includes("保留")'))
        self.js('document.querySelector("#cancel-folder-delete").click()')
        self.assertEqual(len(library.list_folders('transcript')),1)
        self.js('document.querySelector("[data-delete-folder]").click();document.querySelector("#confirm-folder-delete").click()')
        self.until(lambda:not library.list_folders('transcript'))
        self.until(lambda:self.js('window.lulu.state.folders.length')==0)
        self.assertIsNone(library._load_raw(self.eid)['folder_id'])
        self.assertIn('我们每天',library.load_markdown(self.eid))
        self.assertEqual(self.js('window.lulu.state.doc.id'),self.eid)

    def open_folder_delete(self):
        self.widget.refresh()
        self.until(lambda:self.js('window.lulu.state.folders.length') > 0)
        self.js("document.querySelector('#list-menu').click()")
        self.until(lambda:self.js('Boolean(document.querySelector("#manage-folders"))'))
        self.js('document.querySelector("#manage-folders").click()')
        self.js('document.querySelector("[data-delete-folder]").click()')

    def test_delete_folder_with_materials_clears_open_document_and_ignores_search(self):
        folder=library.create_folder('transcript','一起删除')
        library.set_entry_folder(self.eid,folder)
        hidden=library.save_document('不匹配搜索的正文','另一篇',folder_id=folder)
        kept=library.save_document('其他文件夹的正文','保留')
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.js('document.querySelector("#search").value="每天"')
        self.open_folder_delete()
        self.assertTrue(self.js('document.querySelector("#delete-folder-materials").textContent.includes("所属素材")'))
        import os
        from pathlib import Path
        capture=os.environ.get('LULU_PRODUCT_CAPTURE')
        if capture:
            Path(capture).mkdir(parents=True,exist_ok=True)
            self.widget.grab().save(str(Path(capture)/'folder-delete-choices.png'))
        self.js('document.querySelector("#delete-folder-materials").click()')
        self.until(lambda:not library.list_folders('transcript'))
        self.until(lambda:self.js('window.lulu.state.doc === null'))
        self.until(lambda:not self.js('document.querySelector("#dialog").open'))
        self.assertEqual({v['id'] for v in library.list_deleted()},{self.eid,hidden})
        self.assertEqual(library.load_markdown(kept),'其他文件夹的正文')

    def test_partial_failure_is_visible_and_can_retry(self):
        from unittest.mock import patch
        folder=library.create_folder('transcript','部分失败')
        library.set_entry_folder(self.eid,folder)
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt"))'))
        self.open_folder_delete()
        with patch.object(library,'delete',side_effect=OSError('隔离测试：暂时不可写')):
            self.js('document.querySelector("#delete-folder-materials").click()')
            self.until(lambda:self.js('Boolean(document.querySelector("#retry-folder-delete"))'))
            self.assertIn('暂时不可写',self.js('document.querySelector("#dialog").textContent'))
        self.assertIsNotNone(library._load_raw(self.eid))
        self.js('document.querySelector("#retry-folder-delete").click()')
        self.js('document.querySelector("[data-delete-folder]").click()')
        self.js('document.querySelector("#delete-folder-materials").click()')
        self.until(lambda:not library.list_folders('transcript'))
        self.until(lambda:not self.js('document.querySelector("#dialog").open'))

    def test_audio_folder_delete_stops_player_and_preserves_original(self):
        from PySide6.QtCore import QUrl
        from unittest.mock import patch
        import wave
        audio=self.root/'voice.wav'
        with wave.open(str(audio),'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
            f.writeframes(b'\x00\x00' * 1600)
        folder=library.create_folder('voice','声音')
        eid=library.save_voice(title='配音',audio_path=audio,text='文稿',engine='test',
                               voice_name='test',config_summary='test',folder_id=folder)
        self.widget.call('setKind','voice',eid)
        self.until(lambda:self.js('window.lulu.state.kind')=='voice')
        self.until(lambda:self.js('window.lulu.state.doc?.id')==eid)
        # Register a loaded player without starting actual sound playback.
        self.widget.player.setSource(QUrl.fromLocalFile(str(audio)))
        self.widget.playing_id=eid
        self.open_folder_delete()
        with patch.object(self.widget.player,'stop',wraps=self.widget.player.stop) as stop:
            self.js('document.querySelector("#delete-folder-materials").click()')
            self.until(lambda:self.js('window.lulu.state.doc === null'))
            self.until(lambda:not self.js('document.querySelector("#dialog").open'))
            stop.assert_called_once()
        self.assertIsNone(self.widget.playing_id)
        self.assertTrue(self.widget.player.source().isEmpty())
        self.assertTrue(audio.is_file())
        self.assertEqual(library.list_deleted()[0]['id'],eid)
