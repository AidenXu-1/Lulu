from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.core.synthesis import indextts2_worker as worker

class IndexMemoryPolicyTests(unittest.TestCase):

    def test_emotion_conditioning_cache_is_bounded_and_invalidates_changed_tensor(self):
        original = Mock(side_effect=lambda tensor, lengths: object())
        cached = worker._cache_emotion_conditioning(original)
        a, b, c = (SimpleNamespace(_version=0) for _ in range(3))
        lengths = SimpleNamespace(tolist=lambda: [10])
        first = cached(a, lengths)
        self.assertIs(cached(a, lengths), first)
        cached(b, lengths)
        self.assertIs(cached(a, lengths), first)
        a._version += 1
        self.assertIsNot(cached(a, lengths), first)
        cached(c, lengths)
        cached(b, lengths)  # only the latest two immutable conditions are retained
        self.assertEqual(original.call_count, 5)

    def test_inference_disables_training_graphs_for_reference_conditioning(self):
        import torch
        infer = worker._inference_only(lambda: torch.is_grad_enabled())
        with torch.enable_grad():
            self.assertFalse(infer())
            self.assertTrue(torch.is_grad_enabled())

    def test_mac_device_choice_does_not_import_main_app_torch(self):
        from src.core.synthesis import indextts2_subprocess as adapter
        with patch.object(adapter.sys, 'platform', 'darwin'), \
             patch('platform.machine', return_value='arm64'), \
             patch('src.core.engines.local_sensevoice.detect_device', side_effect=AssertionError('heavy import')):
            self.assertEqual(adapter.detect_device(), 'mps')

    def test_25_constructor_preserves_official_reference_loader_and_limits(self):
        import sys
        import torch  # Preserve the native module across patch.dict restoration.
        from types import ModuleType
        loader = Mock(return_value=('audio', 16000))
        tts = SimpleNamespace(_load_and_cut_audio=loader, infer=Mock())
        module = ModuleType('indextts.infer_v2_5')
        module.IndexTTS2 = Mock(return_value=tts)
        package = ModuleType('indextts'); package.infer_v2_5 = module
        with patch.dict(sys.modules, {'indextts': package, 'indextts.infer_v2_5': module}):
            worker._create_tts(model_dir=Path('/unused/model'), device='cpu', model_version='2.5')
        self.assertIs(tts._load_and_cut_audio, loader)
        self.assertEqual(tts._load_and_cut_audio('/voice.wav', 15, True, sr=16000), ('audio', 16000))
        loader.assert_called_with('/voice.wav', 15, True, sr=16000)
        tts._load_and_cut_audio('/emotion.wav', 3)
        loader.assert_called_with('/emotion.wav', 3)

    def test_generation_limit_warning_fails_instead_of_returning_partial_audio(self):
        import warnings
        def infer(**options):
            warnings.warn('WARN: generation stopped due to exceeding `max_mel_tokens` (512).', RuntimeWarning)
        tts = SimpleNamespace(device='cpu', infer=infer)
        with self.assertRaisesRegex(RuntimeError, '未完整读完'):
            worker._safe_infer(tts, text='测试', output_path='/unused.wav')

    def test_lazy_emotion_has_zero_load_until_used_then_releases(self):
        factory = Mock(return_value=SimpleNamespace(inference=lambda _: {'happy': 1}))
        lazy = worker._LazyEmotion(factory, '/model')
        factory.assert_not_called()
        self.assertEqual(lazy.inference('开心'), {'happy': 1})
        factory.assert_called_once_with('/model')
        self.assertIsNone(lazy.backend)
        lazy.release()
        self.assertIsNone(lazy.backend)
        self.assertEqual(lazy.inference('开心'), {'happy': 1})
        factory.assert_called_once()

    def test_emotion_helper_keeps_small_text_model_on_cpu_and_restores_upstream(self):
        import sys
        from types import ModuleType
        module = ModuleType('fixture_emotion')
        model = SimpleNamespace(generate=Mock(return_value='result'))
        original_generate = model.generate
        loader = SimpleNamespace(from_pretrained=Mock(return_value=model))
        module.AutoModelForCausalLM = loader
        def factory(path):
            return SimpleNamespace(model=module.AutoModelForCausalLM.from_pretrained(path, device_map='auto', torch_dtype='float16'))
        factory.__module__ = module.__name__
        with patch.dict(sys.modules, {module.__name__: module}):
            result = worker._cpu_emotion(factory, '/model')
        self.assertIs(module.AutoModelForCausalLM, loader)
        loader.from_pretrained.assert_called_once_with('/model', device_map='cpu', torch_dtype='float16', local_files_only=True)
        result.model.generate(max_new_tokens=32768)
        original_generate.assert_called_once_with(max_new_tokens=256)

    def test_cpu_inference_kwargs_are_unchanged(self):
        tts = SimpleNamespace(device='cpu', infer=Mock(return_value='done'))
        args = dict(text='正常文本', output_path='/unused.wav', verbose=False)
        self.assertEqual(worker._safe_infer(tts, **args), 'done')
        tts.infer.assert_called_once_with(**args)

    def test_old_runtime_factory_is_restored_and_emotion_is_lazy(self):
        import sys
        from types import ModuleType
        module = ModuleType('indextts.infer_v2')
        factory = Mock(return_value=SimpleNamespace(inference=lambda _: {'calm': 1}))
        module.QwenEmotion = factory
        def constructor(**kwargs):
            return SimpleNamespace(qwen_emo=module.QwenEmotion('/model/emotion'), device='cpu', infer=Mock())
        module.IndexTTS2 = constructor
        package = ModuleType('indextts'); package.infer_v2 = module
        with patch.dict(sys.modules, {'indextts': package, 'indextts.infer_v2': module}):
            tts = worker._create_tts(model_dir=Path('/model'), device='cpu')
        self.assertIs(module.QwenEmotion, factory)
        factory.assert_not_called()
        tts.qwen_emo.inference('自然')
        factory.assert_called_once()

    def test_persistent_generation_cleans_up_on_unexpected_monitor_failure(self):
        from src.core.synthesis import indextts2_subprocess as adapter
        proc = SimpleNamespace(pid=12345, poll=lambda: None, stdin=Mock())
        with patch.object(adapter.IndexTTS2PersistentWorkerManager, '_idle_watch_loop'):
            manager = adapter.IndexTTS2PersistentWorkerManager()
        manager._proc = proc
        guard = SimpleNamespace(check=Mock(side_effect=adapter.SynthesisError('测试监控异常')))
        with patch.object(manager, '_ensure_process'), patch.object(manager, '_drop_process') as drop, patch.object(adapter, '_ProcessMemoryMonitor', return_value=guard):
            with self.assertRaisesRegex(adapter.SynthesisError, '测试监控异常'):
                manager.generate({'device':'mps','output':{'temp_wav_path':'/unused.wav'}}, None,
                    lambda *_: None, lambda: False, output_wav=Path('/unused.wav'))
            drop.assert_called_once()
        self.assertFalse(manager._lock.locked())
