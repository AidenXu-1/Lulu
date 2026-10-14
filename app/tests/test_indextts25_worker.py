"""Exercise the public JSON request against a strict external-model substitute."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import wave
from contextlib import redirect_stdout
import torch  # Load outside sys.modules fixtures; native Torch cannot be re-imported.

from src.core.synthesis import indextts2_worker as worker


class IndexTTS25WorkerTests(unittest.TestCase):
    def test_explicit_sampling_choice_reaches_gpt_and_restores_after_failure(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        gpt = SimpleNamespace(inference_speech=Mock(return_value='audio'))
        original = gpt.inference_speech
        # Reproduce pinned upstream: it consumes the option but forwards True.
        def infer(**kwargs):
            gpt.inference_speech(do_sample=True, num_beams=3)
            if kwargs.get('fail'):
                raise RuntimeError('test failure')
            return 'audio'
        tts = SimpleNamespace(model_version='2.5', device='cpu', gpt=gpt, infer=infer)
        self.assertEqual(worker._safe_infer(tts, speech={'do_sample': False}), 'audio')
        original.assert_called_once_with(do_sample=False, num_beams=3)
        self.assertIs(gpt.inference_speech, original)
        with self.assertRaisesRegex(RuntimeError, 'test failure'):
            worker._safe_infer(tts, speech={'do_sample': False}, fail=True)
        self.assertIs(gpt.inference_speech, original)
        worker._safe_infer(tts)
        original.assert_called_with(do_sample=True, num_beams=3)

    def test_quality_cpu_reports_memory_without_fixed_quota(self):
        from types import SimpleNamespace
        from src.core.synthesis import indextts2_subprocess as adapter
        proc = SimpleNamespace(pid=12345, poll=lambda: None)
        with patch.object(adapter.sys, 'platform', 'darwin'):
            monitor = adapter._ProcessMemoryMonitor(proc, 'cpu', model_version='2.5')
        with patch.object(adapter, '_mac_process_memory', return_value=(13 * 1024**3, 1024)):
            monitor.check()
        meta = monitor.annotate({})['meta']
        self.assertNotIn('mps_text_token_limit', meta)
        self.assertNotIn('memory_limit_bytes', meta)
        self.assertEqual(meta['memory_peak_rss_bytes'], 13 * 1024**3)

    def test_mac_request_uses_complete_cpu_path_and_reports_requested_device(self):
        from types import SimpleNamespace
        from src.core.synthesis import indextts2_subprocess as adapter
        from src.core.synthesis.base import SynthesisRequest, VoiceProfile
        status = SimpleNamespace(model_version='2.5', runtime_dir=Path('/unused/runtime'),
                                 model_dir=Path('/unused/model'), snapshot=lambda: {})
        request = SynthesisRequest(text='一整句话不应该因为加速而提前切断。',
            voice=VoiceProfile(id='v', name='V', reference_audio_path='/unused/voice.wav'),
            output_path=Path('/unused/out.wav'), engine_options={'emotion_mode': 'follow'})
        with patch.object(adapter, 'detect_device', return_value='mps'):
            payload = adapter._build_payload(request, status, Path('/unused/staging.wav'), 'test')
        self.assertEqual(payload['device'], 'cpu')
        self.assertEqual(payload['requested_device'], 'mps')
        self.assertEqual(payload['voice']['reference_audio_path'], '/unused/voice.wav')
        self.assertEqual(payload['text'], request.text)
        self.assertEqual(payload['text_segments'], [])
        request.engine_options['do_sample'] = False
        with patch.object(adapter, 'detect_device', return_value='mps'):
            sampling = adapter._build_payload(request, status, Path('/unused/staging.wav'), 'sampling')
        self.assertIs(sampling['speech']['do_sample'], False)
        status.model_version = '2.0'
        with patch.object(adapter, 'detect_device', return_value='mps'):
            self.assertEqual(adapter._build_payload(request, status, Path('/unused/out.wav'), 'old')['device'], 'cpu')

    def test_direct_25_mps_request_cannot_reenable_silent_short_reference(self):
        with self.assertRaisesRegex(ValueError, 'CPU'):
            worker._create_tts(model_dir=Path('/unused/model'), device='mps', model_version='2.5')

    def test_full_official_speed_range_and_random_are_forwarded(self):
        from types import SimpleNamespace
        for speed, duration in ((.5, 2), (1, 1), (2, .5)):
            tts = SimpleNamespace(model_version='2.5', device='cpu')
            with patch.object(worker, '_complete_infer') as infer:
                worker._safe_infer(tts, speech={'language': 'ZH', 'speed': speed, 'random_emotion': True})
                self.assertEqual(infer.call_args.kwargs['duration_factor'], duration)
                self.assertTrue(infer.call_args.kwargs['use_random'])
        for speed in (.49, 2.01, float('nan')):
            with self.assertRaises(ValueError):
                worker._safe_infer(tts, speech={'speed': speed})

    def test_structured_error_reaches_ui_without_exposing_arbitrary_model_text(self):
        from src.core.synthesis.indextts2_subprocess import _worker_error
        from src.core.synthesis.errors import user_error_message
        error = _worker_error({'code': 'RUNTIME_UNAVAILABLE', 'detail_redacted': 'a secret sentence',
                               'message': 'failed'})
        message = user_error_message(error)
        self.assertIn('RUNTIME_UNAVAILABLE', message)
        self.assertIn('模型加载', message)
        self.assertNotIn('secret', message)
        incomplete = _worker_error({'code': 'INCOMPLETE_AUDIO', 'detail_redacted': 'private input'})
        self.assertIn('未完整读完', user_error_message(incomplete))

    def test_official_constructor_language_and_human_speed_reach_model(self):
        calls = []

        class Official25:
            def __init__(self, *, cfg_path, model_dir, use_bf16, device,
                         use_cuda_kernel, use_deepspeed, use_qwen_emo):
                calls.append(('load', use_bf16, use_qwen_emo))
                self.device = device
                self.model_version = 2.5

            def infer(self, *, spk_audio_prompt, text, output_path, lang,
                      duration_factor, verbose):
                calls.append(('infer', lang, duration_factor, text))
                with wave.open(output_path, 'wb') as stream:
                    stream.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                    stream.writeframes(b'\0\0' * 2205)

        package = ModuleType('indextts')
        module = ModuleType('indextts.infer_v2_5')
        module.IndexTTS2 = Official25
        package.infer_v2_5 = module
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), \
             patch.dict(sys.modules, {'indextts': package, 'indextts.infer_v2_5': module}), \
             patch.object(sys, 'path', list(sys.path)):
            root = Path(directory)
            runtime = root / 'runtime'
            vendor = runtime / 'vendor/index-tts'
            vendor.mkdir(parents=True)
            model = root / 'weights'
            model.mkdir()
            reference = root / 'reference.wav'
            reference.touch()
            output = root / 'output.wav'
            request = root / 'request.json'
            request.write_text(json.dumps(dict(
                model_version='2.5', job_id='contract25', runtime_dir=str(runtime),
                runtime_python=sys.executable, vendor_dir=str(vendor), model_dir=str(model),
                voice={'reference_audio_path': str(reference)}, text='こんにちは。',
                output={'temp_wav_path': str(output)}, device='cpu',
                emotion={'control_mode': 'follow'}, speech={'language': 'JA', 'speed': 1.25},
            )))
            with redirect_stdout(io.StringIO()):
                code = worker.run_request(request)
            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())
        self.assertEqual(calls, [('load', False, False), ('infer', 'JA', 0.8, 'こんにちは。')])
