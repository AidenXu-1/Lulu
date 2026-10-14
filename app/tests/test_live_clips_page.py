"""Retired functionality stays unavailable without deleting legacy files."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src.core import library

class LiveClipsRetirementTests(unittest.TestCase):
    def test_generation_modules_are_removed(self):
        for module in ('src.ui.live_clips_page', 'src.core.live_clips', 'src.core.content_output'):
            self.assertIsNone(importlib.util.find_spec(module))
        self.assertFalse(hasattr(library, 'save_live_clip'))

    def test_legacy_clip_body_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(library, 'LIBRARY_DIR', Path(directory)):
            old = Path(directory) / 'old.json'
            old.write_text(json.dumps({'id':'old','kind':'live_clip','title':'旧片段','text':'保留原文'}))
            before = old.read_bytes()
            entry = library.save_document('文稿正文', '文稿')
            output = library.save_rewrite('改写正文', '改写')
            self.assertEqual({e['id'] for e in library.list_entries()}, {entry, output})
            self.assertEqual(old.read_bytes(), before)
            self.assertFalse((Path(directory) / 'docs' / 'old.md').exists())

    def test_public_commands_exclude_retired_clipping(self):
        from src.agent_cli.catalog import capability_discovery
        result = capability_discovery(activated_capability_ids=frozenset({'OUT-01','OUT-02','OUT-03','OUT-04'}))
        self.assertFalse({'OUT-01','OUT-02','OUT-03','OUT-04'} & {c['capability_id'] for c in result['capabilities']})
        self.assertNotIn('live_clip', json.dumps(result))
        from src.agent_cli.main import main
        import io
        for command in (['content','live-clips'], ['content','clip-candidates'], ['content','save','--kind','live_clip']):
            with self.subTest(command=command):
                out = io.StringIO()
                code = main(command, stdout=out, stderr=io.StringIO())
                self.assertNotEqual(code, 0)
