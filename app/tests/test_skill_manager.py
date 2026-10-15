"""Skill lifecycle and package safety; isolated directories, no network or credentials."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.core.agent_workspace import skill_manager as manager, skills, tools
from src.core.agent_workspace.service import WorkspaceService, validate_request
from src.core.agent_workspace.contracts import COMMANDS


class SkillManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(skills, 'skill_root', return_value=self.root / 'skills')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def user_skill(self, identity='my-writing'):
        folder = self.root / ('input-' + identity)
        folder.mkdir()
        (folder / 'SKILL.md').write_text(f'---\nname: {identity}\ndescription: 用户自己的写作方法\n---\n\n保留事实。')
        (folder / 'references').mkdir()
        (folder / 'references/rules.md').write_text('完整参考')
        return folder

    def install_user(self):
        source = self.user_skill()
        inspected = manager.inspect_source(str(source))['entries'][0]
        manager.install(token=inspected['token'])
        return source


    def test_uninstall_survives_restart_and_can_restore_offline(self):
        manager.listing()
        manager.uninstall('lulu-summary')
        self.assertFalse((self.root / 'skills/lulu-summary').exists())
        rows = {r['name']: r for r in manager.listing()['entries']}
        self.assertEqual(rows['lulu-summary']['status'], 'uninstalled')
        self.assertNotIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])
        manager.install('lulu-summary')
        self.assertTrue((self.root / 'skills/lulu-summary/SKILL.md').is_file())
        self.assertEqual(len(list((self.root / 'skill-manager').glob('retired-*'))), 1)

    def test_disable_invalidates_frozen_turn_and_is_durable(self):
        snapshot = skills.catalog()['entries']
        manager.configure('lulu-summary', enabled=False)
        with self.assertRaisesRegex(ValueError, '停用'):
            skills.load('lulu-summary', snapshot)
        self.assertNotIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])
        manager.configure('lulu-summary', enabled=True)
        self.assertIn('lulu-summary', [r['name'] for r in skills.catalog()['entries']])
