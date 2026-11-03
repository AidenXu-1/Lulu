"""SDK constructors are injected; no credentials, network or charges."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

import dashscope
from src.core.engines.dashscope_engine import DashScopeEngine


class DashScopeIsolationTests(unittest.TestCase):
    def test_overlapping_requests_keep_keys_results_and_ids_separate(self):
        barrier = threading.Barrier(2)
        def recognition(**kwargs):
            key = kwargs['api_key']
            def call(path):
                barrier.wait(2)
                return SimpleNamespace(status_code=200, get_sentence=lambda:[dict(text=key, begin_time=0, end_time=100)],
                                       get_request_id=lambda:'request-'+key)
            return SimpleNamespace(call=call)
        engines = [DashScopeEngine(engine_id='dashscope', display_name='测试', model='paraformer-realtime-v2',
                                  api_key='fixture-'+str(i), attempt_limit=1) for i in range(2)]
        with patch('dashscope.audio.asr.Recognition', side_effect=recognition), patch.object(dashscope, 'api_key', 'global-fixture'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda e:e.transcribe(Path('injected.wav')), engines))
            self.assertEqual(dashscope.api_key, 'global-fixture')
        self.assertEqual([r.text for r in results], ['fixture-0', 'fixture-1'])
        self.assertEqual([r.meta['cloud_request']['request_id'] for r in results], ['request-fixture-0','request-fixture-1'])

    def test_bounded_experiment_never_silently_retries(self):
        engine = DashScopeEngine(engine_id='dashscope', display_name='测试', model='paraformer-realtime-v2',
                                 api_key='fixture', attempt_limit=1)
        with patch('dashscope.audio.asr.Recognition', side_effect=RuntimeError('injected')) as call:
            with self.assertRaises(RuntimeError):
                engine.transcribe(Path('injected.wav'))
        self.assertEqual(call.call_count, 1)
