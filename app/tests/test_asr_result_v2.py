"""Isolated result compatibility; never loads models or production settings."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.core import library
from src.core.models import Segment, Transcript
from src.core.output.writer import render
from src.core.transcription_contracts import decode_transcription_result, result_from_transcript
from src.core.transcription_result_store import TranscriptionResultStore, TranscriptionResultStoreError, _canonical_bytes
from src.core.storage_layout import SharedProductStorageLayout


class ResultV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name, value in [('LIBRARY_DIR', self.root / 'library'), ('FOLDERS_FILE', self.root / 'library/folders.json')]:
            p = patch.object(library, name, value); p.start(); self.addCleanup(p.stop)
        self.source = Transcript('你好。再见。', [Segment('你好。', 0, 2, 1), Segment('再见。', 1, 3, 2)],
                                 'zh', 'local_moss_q5', '/fixture.wav', meta={'asr': {
                                     'model_id': 'local_moss_q5', 'diarization': True, 'timestamp_kind': 'segment'}})
        self.store = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(self.root))

    def test_v2_roundtrip_original_bytes_and_overlapping_speakers(self):
        receipt = self.store.write('new', result_from_transcript(self.source))
        raw = self.store.read_verified_bytes('new', receipt['result_ref'])
        result = decode_transcription_result(json.loads(raw), job_id='new')
        self.assertEqual(result.segments, self.source.segments)
        self.assertEqual(result.meta, self.source.meta)
        self.assertEqual(self.store.pending_receipt('new'), receipt)

    def test_v1_recovery_keeps_original_bytes_and_hash(self):
        self.store._ensure_layout()
        payload = dict(schema_id='lulu.transcription-result/v1', schema_revision=1, job_id='old',
            transcript=dict(text='旧稿', segments=[dict(text='旧稿', start=0, end=2)], language='zh', engine='local_qwen3'))
        receipt = self.store._write_payload('old', payload)
        raw = self.store.read_verified_bytes('old', receipt['result_ref'])
        result = decode_transcription_result(json.loads(raw), job_id='old')
        self.assertIsNone(result.segments[0].speaker_id)
        self.assertEqual(raw, _canonical_bytes(payload))
        self.assertEqual(receipt, self.store.pending_receipt('old'))

    def test_rejects_invalid_new_fields_without_relaxing_v1(self):
        receipt = self.store.write('new', result_from_transcript(self.source))
        payload = json.loads(self.store.read_verified_bytes('new', receipt['result_ref']))
        for changes in ({'speaker_id': True}, {'speaker_id': 0}, {'start': float('nan')}, {'end': None}, {'end': -1}):
            changed = json.loads(json.dumps(payload)); changed['transcript']['segments'][0].update(changes)
            with self.assertRaises(ValueError): decode_transcription_result(changed, job_id='new')
        payload['schema_id'] = 'lulu.transcription-result/v1'
        with self.assertRaises(ValueError): decode_transcription_result(payload, job_id='new')

    def test_saved_structure_rejects_changed_speaker_with_same_text(self):
        library.save(self.source, '访谈', entry_id='same')
        self.source.segments[0].speaker_id = 2
        with self.assertRaises(ValueError): library.save(self.source, '访谈', entry_id='same')

    def test_legacy_save_recovery_preserves_edited_body_and_checks_old_segments(self):
        old = Transcript('原文', [Segment('原文', 0, 1)], 'zh', 'local_qwen3', '/old.wav')
        library.save(old, '旧稿', entry_id='old')
        row = library._load_raw('old'); row.pop('result_digest_version')
        row['result_digest'] = hashlib.sha256(json.dumps([old.source, old.text, old.engine], ensure_ascii=False).encode()).hexdigest()
        for s in row['segments']: s.pop('speaker_id')
        library._write_raw(row)
        library.save_markdown('old', '用户编辑')
        self.assertEqual(library.save(old, '旧稿', entry_id='old'), 'old')
        library.finish_transcript_document('old', old, '旧稿')
        self.assertEqual(library.load_markdown('old'), '用户编辑')
        old.segments[0].end = 2
        with self.assertRaises(ValueError): library.save(old, '旧稿', entry_id='old')

    def test_document_finish_reopens_speakers_and_never_overwrites_edits(self):
        library.save(self.source, '访谈', entry_id='moss')
        library.finish_transcript_document('moss', self.source, '访谈')
        reopened = library.load('moss')
        self.assertIn('发言人1：你好。', reopened.text)
        self.assertEqual(reopened.segments, self.source.segments)
        self.assertIn('发言人2：再见。', render(reopened, 'srt'))
        library.save_markdown('moss', '改好的稿子')
        library.finish_transcript_document('moss', self.source, '访谈')
        edited = library.load('moss')
        self.assertEqual(edited.text, '改好的稿子')
        self.assertTrue(edited.meta['segments_stale'])
        with self.assertRaises(ValueError): render(edited, 'srt')

    def test_srt_refuses_missing_or_zero_intervals_instead_of_dropping_words(self):
        for segment in (Segment('缺时间'), Segment('零长度', 1, 1)):
            value = Transcript('完整正文', [Segment('有效', 0, 1), segment])
            with self.assertRaises(ValueError): render(value, 'srt')

    def test_q8_srt_uses_real_platform_subtitles_or_refuses_inference(self):
        from src.core import pipeline
        source='https://example.test/fixture'
        sub=Transcript('字幕',[Segment('字幕',0,1)],engine='platform_subtitles')
        with patch('src.core.subtitles.fetch_from_link',return_value=sub), \
             patch.object(pipeline,'get_engine') as build:
            result=pipeline.transcribe_source(source,engine_name='local_sensevoice_q8',
                prefer_subtitles=True,required_format='srt')
            self.assertIs(result,sub);build.assert_not_called()
        with patch('src.core.subtitles.fetch_from_link',return_value=None), \
             patch.object(pipeline,'resolve_source',return_value=self.root/'audio.wav'), \
             patch.object(pipeline,'to_wav_16k_mono',return_value=self.root/'audio.wav'), \
             patch.object(pipeline,'get_engine') as build:
            with self.assertRaisesRegex(ValueError,'不提供时间点'):
                pipeline.transcribe_source(source,engine_name='local_sensevoice_q8',
                    prefer_subtitles=True,required_format='srt')
            build.assert_not_called()


if __name__ == '__main__': unittest.main()
