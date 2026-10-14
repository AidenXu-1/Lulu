"""Retiring one model must not remove shared voices or the retained engines."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest

from src.agent_cli.catalog import load_capability_catalog
from src.core.synthesis import registry, runtime_packages


class GPTSoVITSRetirementTests(unittest.TestCase):
    def test_removed_engine_is_unavailable_and_other_engines_remain(self):
        self.assertNotIn('local_gptsovits', runtime_packages.TTS_RUNTIMES)
        with self.assertRaisesRegex(ValueError, '未知合成引擎'):
            registry.get_synthesis_engine('local_gptsovits')
        for name in ('local_indextts2', 'local_voxcpm2'):
            self.assertIn(name, runtime_packages.TTS_RUNTIMES)
            self.assertIsNotNone(registry.get_synthesis_engine(name))
        for module in ('gptsovits_worker', 'gptsovits_subprocess', 'gptsovits_finetune', 'gptsovits_weights', 'training_wheel_lock'):
            self.assertIsNone(importlib.util.find_spec('src.core.synthesis.' + module))

    def test_catalog_removes_retired_capabilities_and_binds_actual_sources(self):
        root = Path(__file__).resolve().parents[2]
        snapshot = load_capability_catalog()
        source = snapshot['source_catalog']
        source_bytes = (root / source['path']).read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), source['sha256'])
        normative = re.search(r'```json\n(.*?)\n```', source_bytes.decode(), re.S).group(1)
        self.assertEqual(hashlib.sha256(normative.encode()).hexdigest(), source['normative_json_sha256'])
        self.assertEqual(json.loads(normative), snapshot['catalog'])
        matrix = snapshot['catalog']['source_matrix']
        self.assertEqual(hashlib.sha256((root / matrix['path']).read_bytes()).hexdigest(), matrix['sha256'])
        capabilities = snapshot['catalog']['capabilities']
        retired = {'TTS-04', 'TTS-10', 'TTS-11A', 'TTS-11B', 'TTS-12', 'TTS-13'}
        self.assertTrue(retired.isdisjoint(item['capability_id'] for item in capabilities))
        self.assertEqual(len(capabilities), matrix['expected_capability_count'])
        self.assertNotIn('local_gptsovits', json.dumps(snapshot))
