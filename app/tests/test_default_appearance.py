"""Retired preferences must retain unrelated configuration and use Lulu paper."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src import config
from src.ui import theme


class DefaultAppearanceTests(unittest.TestCase):
    def test_retired_themes_load_as_paper_without_rewriting_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for old_theme in ('light', 'dark', 'warm', 'unknown'):
                payload = dict(theme=old_theme, engine_name='local_qwen3',
                               models_dir='/fixture/models', media_dir='/fixture/media',
                               feishu_table_targets=[{'name': '原资料库', 'app_token': 'fixture', 'table_id': 'fixture'}])
                original = json.dumps(payload, ensure_ascii=False).encode()
                path.write_bytes(original)
                with patch.object(config, '_CONFIG_PATH', path):
                    cfg = config.AppConfig.load()
                    self.assertEqual(cfg.theme, 'warm')
                    self.assertEqual(cfg.engine_name, payload['engine_name'])
                    self.assertEqual(cfg.models_dir, payload['models_dir'])
                    self.assertEqual(cfg.media_dir, payload['media_dir'])
                    self.assertEqual(path.read_bytes(), original)
                    self.assertTrue(cfg.save())
                saved = json.loads(path.read_text())
                self.assertEqual(saved['theme'], 'warm')
                self.assertEqual(saved['feishu_table_targets'], cfg.feishu_table_targets)
                self.assertEqual(saved['models_dir'], payload['models_dir'])

    def test_constructor_and_save_cannot_reintroduce_retired_theme(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(config, '_CONFIG_PATH', Path(directory) / 'config.json'):
            cfg = config.AppConfig(theme='dark')
            self.assertEqual(cfg.theme, 'warm')
            cfg.theme = 'light'
            self.assertTrue(cfg.save())
            self.assertEqual(cfg.theme, 'warm')

    def test_only_paper_palette_remains_and_legacy_names_fall_back_safely(self):
        self.assertEqual(set(theme.THEMES), {'warm'})
        self.assertEqual(set(theme.CONSUMER_THEMES), {'warm'})
        for name in ('warm', 'light', 'dark', 'unknown'):
            self.assertIs(theme.apply_theme(name), theme.WARM)
            self.assertIs(theme.consumer_theme(name), theme.CONSUMER_THEMES['warm'])
