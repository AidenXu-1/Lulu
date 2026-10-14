"""Quality policy at the adapter/worker boundaries; no models or user data."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.core.synthesis import indextts2_subprocess as adapter
from src.core.synthesis import indextts2_worker as worker
from src.core.synthesis.base import SynthesisRequest, VoiceProfile


class IndexQualityPolicyTests(unittest.TestCase):
    def test_mac_default_and_explicit_mps_use_the_gui_quality_path(self):
        for version in ('2.0', '2.5'):
            for device in (None, 'mps', 'cpu'):
                with self.subTest(version=version, device=device):
                    options = {'model_version': version, 'emotion_mode': 'follow'}
                    if device:
                        options['device'] = device
                    request = SynthesisRequest('保持一句话的完整表达。',
                        VoiceProfile('v', 'V', '/unused/reference.wav'), Path('/unused/out.wav'),
                        engine_options=options)
                    status = SimpleNamespace(model_version=version, runtime_dir=Path('/unused/runtime'),
                                             model_dir=Path('/unused/model'), snapshot=lambda: {})
                    with patch.object(adapter, 'detect_device', return_value='mps'):
                        payload = adapter._build_payload(request, status, Path('/unused/stage.wav'), 'test')
                    self.assertEqual(payload['device'], 'cpu')
                    self.assertEqual(payload['text'], request.text)
                    self.assertEqual(payload['text_segments'], [])
                    self.assertEqual(payload['voice']['reference_audio_path'], '/unused/reference.wav')

    def test_full_parameters_are_not_silently_reduced(self):
        tts = SimpleNamespace(model_version='2.0', device='mps')
        generation = {'num_beams': 3, 'max_text_tokens_per_segment': 120, 'max_mel_tokens': 1500}
        with patch.object(worker, '_complete_infer', return_value='audio') as infer:
            self.assertEqual(worker._safe_infer(tts, speech={'generation': generation},
                                               text='整句。', output_path='/unused.wav'), 'audio')
        self.assertEqual(infer.call_args.kwargs,
                         {'text': '整句。', 'output_path': '/unused.wav', **generation})

    def test_model_memory_over_old_limit_is_observed_without_killing(self):
        for version in ('2.0', '2.5'):
            with self.subTest(version=version), patch.object(adapter.sys, 'platform', 'darwin'):
                guard = adapter._ProcessMemoryMonitor(SimpleNamespace(pid=123, poll=lambda: None),
                                               'cpu', model_version=version)
                with patch.object(adapter, '_mac_process_memory', return_value=(16*1024**3, 15*1024**3)):
                    guard.check()
                meta = guard.annotate({})['meta']
                self.assertEqual(meta['memory_peak_footprint_bytes'], 15*1024**3)
                self.assertNotIn('memory_limit_bytes', meta)

    def test_unavailable_memory_measurement_does_not_abort_audio(self):
        with patch.object(adapter.sys, 'platform', 'darwin'):
            guard = adapter._ProcessMemoryMonitor(SimpleNamespace(pid=123, poll=lambda: None), 'cpu')
        with patch.object(adapter, '_mac_process_memory', side_effect=OSError('unavailable')):
            guard.check()
        self.assertFalse(guard.annotate({})['meta']['memory_measurement_available'])

    def test_legacy_direct_mps_cannot_bypass_quality_routing(self):
        for version in ('2.0', '2.5'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'CPU'):
                worker._create_tts(model_dir=Path('/unused/model'), device='mps', model_version=version)

    def test_no_training_graphs_for_either_model(self):
        import torch
        from types import ModuleType
        import sys
        for version, module_name in [('2.0', 'infer_v2'), ('2.5', 'infer_v2_5')]:
            tts = SimpleNamespace(infer=lambda: torch.is_grad_enabled())
            module = ModuleType('indextts.' + module_name)
            module.IndexTTS2 = Mock(return_value=tts)
            package = ModuleType('indextts'); setattr(package, module_name, module)
            with patch.dict(sys.modules, {'indextts': package, 'indextts.'+module_name: module}):
                result = worker._create_tts(model_dir=Path('/unused/model'), device='cpu', model_version=version)
            with torch.enable_grad():
                self.assertFalse(result.infer())
                self.assertTrue(torch.is_grad_enabled())
