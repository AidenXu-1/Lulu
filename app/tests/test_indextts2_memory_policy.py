from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.core.synthesis import indextts2_worker as worker

class IndexMemoryPolicyTests(unittest.TestCase):


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
