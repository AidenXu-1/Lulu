import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis import indextts2_worker as worker


class IndexCacheIsolationTests(unittest.TestCase):
    def test_inference_caches_cannot_modify_sealed_runtime(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
             patch.object(sys, 'path', list(sys.path)), patch.object(sys, 'dont_write_bytecode', False):
            root = Path(temp).resolve()
            runtime = root/'runtime'
            work = root/'work'
            os.environ['NUMBA_CACHE_DIR'] = str(runtime/'old-cache')
            _, _, _, _, env = worker._ensure_runtime_paths({'runtime_dir':str(runtime)}, work)
            cache = Path(env['NUMBA_CACHE_DIR'])
            self.assertTrue(cache.is_relative_to(work))
            self.assertEqual(os.environ['NUMBA_CACHE_DIR'], str(cache))
            self.assertTrue(sys.dont_write_bytecode)
            self.assertFalse(runtime.exists())
