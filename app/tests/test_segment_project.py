import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from src.core.synthesis.base import VoiceProfile
from src.core.synthesis.segment_project import (
    SegmentProject, ProjectCancelled, digest, inspect_wav, paragraphs,
)


def wav(path, value=1200, frames=8000, rate=8000):
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<h', value) * frames)


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.fail = False

    def synthesize(self, request, progress, cancelled):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError('模拟生成失败')
        wav(request.output_path, 1000 + len(self.calls), frames=8000 + len(self.calls) * 10)
        return SimpleNamespace(wav_path=request.output_path, meta={'model_version': '2.5'})


class SegmentProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reference = self.root / 'reference.wav'
        wav(self.reference)
        self.voice = VoiceProfile('voice', '测试音色', str(self.reference))
        self.project = SegmentProject.create(self.root, '第一段完整的意思。\n\n第二段继续说明。', self.voice, {'emotion_mode': 'follow'})
        self.ids = [p['id'] for p in self.project.read()['parts']]
        self.engine = FakeEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def test_reopen_reference_snapshot_and_retry_preserve_selected_audio(self):
        old = self.project.generate(self.ids[0], self.engine)
        other = self.project.generate(self.ids[1], self.engine)
        new = self.project.generate(self.ids[0], self.engine)
        reopened = SegmentProject(self.project.path)
        data = reopened.read()
        self.assertEqual(data['parts'][0]['selected'], old['id'])
        self.assertEqual(data['parts'][1]['selected'], other['id'])
        self.assertEqual(digest(reopened.checked_audio(old)), old['sha256'])
        reopened.choose(self.ids[0], new['id'])
        reopened.choose(self.ids[0], old['id'])
        self.assertEqual(reopened.read()['parts'][0]['selected'], old['id'])
        self.reference.unlink()
        reopened.generate(self.ids[0], self.engine)  # reference copy survives library changes
        self.assertEqual(self.engine.calls[-1].engine_options['model_version'], '2.5')
        self.assertNotEqual(self.engine.calls[-1].voice.reference_audio_path, str(self.reference))

    def test_edit_invalidates_only_one_segment_and_old_version_restores_text(self):
        old = self.project.generate(self.ids[0], self.engine)
        self.project.generate(self.ids[1], self.engine)
        self.project.edit(self.ids[0], '修改后的文案。')
        self.assertIsNone(self.project.current(self.project.read()['parts'][0]))
        with self.assertRaisesRegex(ValueError, '修改'):
            self.project.export(self.root / 'incomplete.wav')
        self.assertTrue(self.project.checked_audio(old).exists())
        self.project.choose(self.ids[0], old['id'])
        self.assertEqual(self.project.read()['parts'][0]['text'], '第一段完整的意思。')

    def test_failure_and_cancellation_keep_prior_results(self):
        first = self.project.generate(self.ids[0], self.engine)
        before = self.project.path.read_bytes()
        self.engine.fail = True
        with self.assertRaises(RuntimeError):
            self.project.generate(self.ids[1], self.engine)
        self.assertEqual(before, self.project.path.read_bytes())
        with self.assertRaises(ProjectCancelled):
            self.project.generate(self.ids[1], self.engine, cancelled=lambda: True)
        self.assertEqual(before, self.project.path.read_bytes())
        self.assertTrue(self.project.checked_audio(first).exists())

    def test_exact_pcm_join_and_exclusive_export(self):
        versions = [self.project.generate(i, self.engine) for i in self.ids]
        target = self.root / 'joined.wav'
        result = self.project.export(target, .25)
        with wave.open(str(target)) as f:
            actual = f.readframes(f.getnframes())
        expected = []
        for v in versions:
            with wave.open(str(self.project.checked_audio(v))) as f:
                expected.append(f.readframes(f.getnframes()))
        self.assertEqual(actual, expected[0] + b'\0\0' * 2000 + expected[1])
        self.assertEqual(result['sha256'], digest(target))
        self.assertEqual(SegmentProject(self.project.path).read()['gap_seconds'], .25)
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            self.project.export(target)
        self.assertEqual(before, target.read_bytes())

    def test_export_cancel_and_write_failure_preserve_originals(self):
        versions = [self.project.generate(i, self.engine) for i in self.ids]
        before = self.project.path.read_bytes()
        target = self.root / 'cancelled.wav'
        with self.assertRaises(ProjectCancelled):
            self.project.export(target, cancelled=lambda: True)
        self.assertFalse(target.exists())
        with patch('src.core.synthesis.segment_project.os.link', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.project.export(target)
        self.assertFalse(target.exists())
        self.assertEqual(before, self.project.path.read_bytes())
        for v in versions:
            self.project.checked_audio(v)

    def test_reference_and_audio_tampering_block_reuse(self):
        version = self.project.generate(self.ids[0], self.engine)
        ref = self.project.file(self.project.read()['references']['voice']['path'])
        wav(ref, value=2000)
        with self.assertRaisesRegex(ValueError, '参考音频'):
            self.project.generate(self.ids[1], self.engine)
        wav(self.project.file(version['path']), value=3000)
        with self.assertRaisesRegex(ValueError, '改动'):
            self.project.choose(self.ids[0], version['id'])

    def test_stale_edit_and_atomic_save_failure(self):
        original = self.project.read()
        self.project.edit(self.ids[0], '另外一个窗口修改。')
        with self.assertRaisesRegex(ValueError, '别处'):
            self.project.edit(self.ids[0], '旧窗口内容。', original['revision'])
        before = self.project.path.read_bytes()
        with patch('src.core.synthesis.segment_project.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.project.edit(self.ids[0], '保存失败。')
        self.assertEqual(before, self.project.path.read_bytes())

    def test_measurement_hints_do_not_claim_naturalness(self):
        path = self.root / 'silence.wav'
        with wave.open(str(path), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            f.writeframes(struct.pack('<h', 1000) * 4000 + b'\0\0' * 12000 + struct.pack('<h', 32767) * 4000)
        info = inspect_wav(path)
        silent = next(w for w in info['warnings'] if w['kind'] == 'silence')
        self.assertAlmostEqual(silent['start'], .5)
        self.assertAlmostEqual(silent['end'], 2)
        self.assertIn('请试听', silent['message'])
        self.assertTrue(any(w['kind'] == 'full_scale' for w in info['warnings']))
        wav(path, value=0)
        with self.assertRaises(ValueError):
            inspect_wav(path)

    def test_paragraphs_are_explicit_not_split_at_every_sentence(self):
        self.assertEqual(paragraphs('第一句。第二句。\n\n第三句。'), ['第一句。第二句。', '第三句。'])
        with self.assertRaises(ValueError):
            paragraphs('长' * 601)
        with self.assertRaises(ValueError):
            self.project.file('../outside.wav')

    def test_mismatched_audio_format_blocks_export_without_touching_versions(self):
        for i in self.ids:
            self.project.generate(i, self.engine)
        data = self.project.read()
        version = data['parts'][1]['versions'][0]
        wav(self.project.file(version['path']), rate=16000)
        version['sha256'] = digest(self.project.file(version['path']))
        self.project.save(data, data['revision'])
        with self.assertRaisesRegex(ValueError, '格式不同'):
            self.project.export(self.root / 'invalid.wav')
        self.assertFalse((self.root / 'invalid.wav').exists())


if __name__ == '__main__':
    unittest.main()
