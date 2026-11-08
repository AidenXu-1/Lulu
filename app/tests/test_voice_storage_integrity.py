"""Owned temporary audio/index fixtures only; no models, network or credentials."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from src.core.synthesis import voices

class VoiceStorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.old=self.root/'voices';self.old.mkdir()
        self.patches=[patch.object(voices,'VOICES_DIR',self.old),patch.object(voices,'INDEX',self.old/'voices.json')]
        for item in self.patches:item.start()
        self.sample=self.root/'sample.wav';self.sample.write_bytes(b'owned test reference')
        self.voice=voices.add_voice('Original',str(self.sample))
        self.before=voices.INDEX.read_bytes()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def test_bad_index_never_becomes_an_empty_writable_library(self):
        for broken in (b'{broken',b'{}',b'[null]',self.before[:-8]):
            for operation in (lambda:voices.add_voice('New',str(self.sample)),
                              lambda:voices.update_voice(self.voice.id,name='Changed'),
                              lambda:voices.delete_voice(self.voice.id)):
                with self.subTest(broken=broken,operation=operation):
                    voices.INDEX.write_bytes(broken)
                    with self.assertRaises(ValueError):operation()
                    self.assertEqual(voices.INDEX.read_bytes(),broken)
                    self.assertEqual(Path(self.voice.reference_audio_path).read_bytes(),self.sample.read_bytes())

    def test_missing_or_unreadable_index_preserves_existing_reference(self):
        backup=self.root/'index-backup.json';voices.INDEX.rename(backup)
        with self.assertRaises(ValueError):voices.add_voice('New',str(self.sample))
        self.assertFalse(voices.INDEX.exists());self.assertTrue(Path(self.voice.reference_audio_path).is_file())
        backup.rename(voices.INDEX)
        with patch.object(Path,'read_text',side_effect=PermissionError('owned test denial')):
            with self.assertRaises(ValueError):voices.delete_voice(self.voice.id)
        self.assertEqual(voices.INDEX.read_bytes(),self.before)

    def test_partial_index_keeps_readable_rows_but_blocks_changes(self):
        records=json.loads(self.before)+[{'id':'incomplete'}]
        voices.INDEX.write_text(json.dumps(records));before=voices.INDEX.read_bytes()
        self.assertEqual([row.id for row in voices.list_voices()],[self.voice.id])
        with self.assertRaises(ValueError):voices.update_voice(self.voice.id,name='Changed')
        self.assertEqual(voices.INDEX.read_bytes(),before)

    def test_relocation_uses_new_copy_even_when_old_directory_is_unavailable(self):
        target=self.root/'new-voices';voices.relocate_voice_library(target)
        self.old.rename(self.root/'preserved-old-voices')
        value=voices.get_voice(self.voice.id)
        self.assertTrue(Path(value.reference_audio_path).is_relative_to(target))
        self.assertEqual(Path(value.reference_audio_path).read_bytes(),self.sample.read_bytes())

    def test_existing_broken_relocation_target_does_not_change_active_library(self):
        target=self.root/'bad-target';target.mkdir();(target/'voices.json').write_text('{broken')
        with self.assertRaises(ValueError):voices.relocate_voice_library(target)
        self.assertEqual(voices.current_voice_library_dir(),self.old)
        self.assertEqual(voices.INDEX.read_bytes(),self.before)

    def test_relocation_write_probe_never_overwrites_an_existing_file(self):
        target=self.root/'existing-library';target.mkdir()
        (target/'voices.json').write_text('[]')
        protected=target/'.lulu_write_test';protected.write_bytes(b'owned but unrelated fixture')
        voices.relocate_voice_library(target)
        self.assertEqual(protected.read_bytes(),b'owned but unrelated fixture')
        self.assertTrue(Path(self.voice.reference_audio_path).is_file())

    def test_delete_identifier_cannot_escape_voice_directory(self):
        sibling=self.root/'outside';sibling.mkdir();(sibling/'keep.txt').write_text('keep')
        with self.assertRaises(ValueError):voices.delete_voice('../outside')
        self.assertEqual((sibling/'keep.txt').read_text(),'keep')
        self.assertEqual(voices.INDEX.read_bytes(),self.before)

    def test_model_identity_matches_engine_and_legacy_read_does_not_rewrite_index(self):
        from src.core.synthesis.runtime_packages import TTS_RUNTIMES
        for engine,spec in TTS_RUNTIMES.items():
            row=voices.add_voice(engine,str(self.sample),engine=engine)
            self.assertEqual(row.model_id,spec.weights_repo)
        legacy=json.loads(voices.INDEX.read_text());legacy[0]['model_id']='openbmb/VoxCPM2'
        voices.INDEX.write_text(json.dumps(legacy));before=voices.INDEX.read_bytes()
        row=voices.get_voice(legacy[0]['id'])
        self.assertEqual(row.model_id,TTS_RUNTIMES[row.engine].weights_repo)
        self.assertEqual(voices.INDEX.read_bytes(),before)

if __name__=='__main__':unittest.main()
