"""Local PCM fixtures; no runtime, model, user configuration or network."""
import array
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from src.core.audio.output_level import level_pcm16_wav
from src.core.synthesis import indextts2_subprocess as engine
from src.core.synthesis.base import SynthesisRequest, VoiceProfile, SynthesisError
from src.core.synthesis.indextts2_results import build_result_summary


def write_audio(path, samples, channels=1):
    with wave.open(str(path), 'wb') as f:
        f.setparams((channels, 2, 22050, 0, 'NONE', 'not compressed'))
        f.writeframes(array.array('h', samples).tobytes())


def read_audio(path):
    with wave.open(str(path), 'rb') as f:
        params = f.getparams()
        data = array.array('h', f.readframes(f.getnframes()))
    return params, data


class OutputLevelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio.wav'

    def test_quiet_speech_keeps_timing_and_relative_dynamics(self):
        samples = [int(220 * math.sin(i * .13)) for i in range(22050)]
        samples += [0] * 3000 + [v // 2 for v in samples]
        write_audio(self.path, samples)
        params, before = read_audio(self.path)
        meta = level_pcm16_wav(self.path)
        after_params, after = read_audio(self.path)
        self.assertEqual(params, after_params)
        rms = math.sqrt(sum(v*v for v in after) / len(after)) / 32768
        self.assertAlmostEqual(20 * math.log10(rms), -24, places=2)
        gain = 10 ** (meta['gain_db'] / 20)
        self.assertLessEqual(max(abs(b * gain - a) for b, a in zip(before, after)), .501)
        self.assertEqual(list(after[22050:25050]), [0] * 3000)
        self.assertTrue(meta['applied'])

    def test_peak_limit_uses_one_gain_for_all_samples_and_channels(self):
        samples = [20, -10] * 5000 + [32767, -32768]
        write_audio(self.path, samples, channels=2)
        meta = level_pcm16_wav(self.path)
        params, after = read_audio(self.path)
        self.assertEqual(params.nchannels, 2)
        self.assertLessEqual(max(abs(v) for v in after)/32768, 10**(-1/20))
        self.assertTrue(meta['peak_limited'])
        gain = 10**(meta['gain_db']/20)
        self.assertLessEqual(max(abs(a-b*gain) for a,b in zip(after,samples)), .501)

    def test_silence_and_near_silence_are_not_amplified(self):
        for samples in ([0]*10000, [1,-1]*5000):
            write_audio(self.path, samples)
            original = self.path.read_bytes()
            meta = level_pcm16_wav(self.path)
            self.assertFalse(meta['applied'])
            self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_and_truncated_audio_leave_original_untouched(self):
        for content in (b'not wav', b''):
            self.path.write_bytes(content)
            with self.assertRaises((ValueError, wave.Error, EOFError)):
                level_pcm16_wav(self.path)
            self.assertEqual(self.path.read_bytes(), content)
        write_audio(self.path, [100] * 10000)
        self.path.write_bytes(self.path.read_bytes()[:-20])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            level_pcm16_wav(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cancel_during_write_or_publish_preserves_original(self):
        for stop_at in (1, 5, 8, 13):
            write_audio(self.path, [200,-200]*50000)
            before = self.path.read_bytes()
            calls = 0
            def check():
                nonlocal calls
                calls += 1
                if calls == stop_at:
                    raise engine._Cancelled()
            with self.assertRaises(engine._Cancelled):
                level_pcm16_wav(self.path, check_cancelled=check)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_replace_failure_preserves_original_and_cleans_temporary(self):
        write_audio(self.path, [200,-200]*10000)
        before = self.path.read_bytes()
        with patch('src.core.audio.output_level.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                level_pcm16_wav(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def run_engine(self, version, persistent=False, output_format='wav'):
        ref = self.path.parent / 'ref.wav'
        write_audio(ref, [200,-200]*100)
        request = SynthesisRequest(text='第一句。第二句。', voice=VoiceProfile(
            id='isolated', name='fixture', reference_audio_path=str(ref)),
            output_path=self.path, output_format=output_format,
            engine_options={'model_version': version, 'use_persistent_worker': persistent})
        status=SimpleNamespace(ready=True, model_version=version, runtime_dir=Path('/isolated/runtime'),
            model_dir=Path('/isolated/weights'), snapshot=lambda: {'ready':True})
        fractions = []
        self.generated_fractions = fractions
        def generate(*args, **kwargs):
            args[4]('raw complete', kwargs.get('span_fraction', 1.0))
            write_audio(self.path, [200,-200]*10000)
            return {'duration_seconds':20000/22050, 'meta':{}}
        selected = '_synthesize_persistent' if persistent else '_synthesize_once'
        with patch.object(engine, 'check_runtime', return_value=status), patch.object(
                engine, '_build_payload', return_value={'model_version':version}), patch.object(
                engine.IndexTTS2SubprocessEngine, selected, side_effect=generate):
            return engine.IndexTTS2SubprocessEngine().synthesize(
                request, progress=lambda message, fraction: fractions.append(fraction))

    def test_both_worker_routes_level_both_versions(self):
        for persistent in (False, True):
            for version in ('2.0','2.5'):
                with self.subTest(version=version,persistent=persistent):
                    result = self.run_engine(version,persistent)
                    _, samples = read_audio(self.path)
                    self.assertIn('output_level', result.meta)
                    self.assertGreater(max(samples), 1500)
                    self.assertEqual(self.generated_fractions, [.94, .95, 1.0])

    def test_mp3_conversion_receives_adjusted_wav(self):
        seen=[]
        def convert(src,dst):
            seen.append(max(read_audio(src)[1]))
            return dst
        with patch('src.core.audio.converter.wav_to_mp3', side_effect=convert):
            self.run_engine('2.5',output_format='mp3')
        self.assertGreater(seen[0],1500)

    def test_failure_is_visible_not_silent_success(self):
        with patch('src.core.synthesis.indextts2_subprocess.level_pcm16_wav',side_effect=OSError('disk')):
            with self.assertRaisesRegex(SynthesisError, '音量'):
                self.run_engine('2.5')

    def test_summary_only_labels_actual_processing(self):
        for version, applied, expected in [('2.5',True,True),('2.5',False,False),('2.0',True,True),('2.0',False,False)]:
            summary=build_result_summary(output_path=self.path,voice_label='测试',style_meta={},
                result_meta={'model_version':version,'output_level':{'applied':applied}},
                duration_seconds=1,glossary_applied=False)
            self.assertEqual('音量已调整' in summary['summary'],expected)
