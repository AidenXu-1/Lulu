"""Trash disposal and selection use temporary libraries, never the installed App."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests import test_library_workspace_core as core_tests
from src.core import library, trash_cleanup


class TrashDisposalTests(unittest.TestCase):
    setUp = core_tests.WorkspaceCoreTests.setUp

    def deleted(self, title='文稿'):
        eid = library.save_document('第一版', title)
        library.save_markdown(eid, '第二版')
        library.delete(eid)
        return next(r for r in library.list_deleted() if r['id'] == eid)

    def date(self, row, value):
        p = library.LIBRARY_DIR / '.trash' / (row['token'] + '.json')
        data = json.loads(p.read_text()); data['deleted_at'] = value
        p.write_text(json.dumps(data)); return p


    def test_archive_preview_reads_real_metadata_and_full_history_snapshots(self):
        folder=library.create_folder('transcript','创作资料')
        eid=library.save_document('第一版正文','归档文稿')
        library.set_entry_folder(eid,folder)
        library.save_markdown(eid,'第二版正文')
        library.delete(eid)
        row=next(row for row in library.list_deleted() if row['id']==eid)
        self.assertEqual(row['text_preview'],'第二版正文')
        preview=library.deleted_preview(row['token'])
        self.assertEqual(preview['folder_name'],'创作资料')
        self.assertEqual(preview['version_count'],1)
        self.assertEqual(preview['text'],'第二版正文')
        self.assertEqual(library.read_version(eid,library.list_versions(eid)[0]['name']),'第一版正文')

    def test_archive_audio_preview_is_read_only_and_reports_real_file(self):
        audio=self.root/'voice.wav'; audio.write_bytes(b'original audio')
        eid=library.save_voice(title='配音',audio_path=audio,text='文字',engine='test',voice_name='音色',config_summary='test',duration_seconds=2.5)
        library.delete(eid)
        row=library.list_deleted()[0]
        preview=library.deleted_preview(row['token'],include_audio=True)
        self.assertEqual(preview['id'],eid)
        self.assertEqual(preview['audio_path'],str(audio))
        self.assertEqual(preview['audio_bytes'],len(b'original audio'))
        self.assertFalse(preview['audio_missing'])
        self.assertEqual(audio.read_bytes(),b'original audio')
