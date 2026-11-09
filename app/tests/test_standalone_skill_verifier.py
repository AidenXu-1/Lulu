"""Signed temporary Apps and in-memory keys; never touch installed state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_standalone_install_source as fixtures
from test_generation9_formal_install_public import MemoryKeys
from src.core.agent_entry.standalone_state import prepare_install_state

ROOT = Path(__file__).resolve().parents[2]


def load(path):
    spec = importlib.util.spec_from_file_location('standalone_verifier_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StandaloneSkillVerifierTests(unittest.TestCase):
    def setUp(self):
        fixtures.StandaloneInstallSourceTests.setUp(self)
        fixtures.StandaloneInstallSourceTests.prepare(self)
        self.product = self.root/'product'
        self.product.mkdir()
        self.state = prepare_install_state(self.app, product_root=self.product, keys=MemoryKeys())
        self.launcher = self.root/'lulu'
        self.launcher.write_bytes(('#!/bin/sh\nset -eu\nexec '+str(self.app/'Contents/MacOS/lulu-agent-cli')+' "$@"\n').encode())
        self.launcher.chmod(0o700)
        self.skill = self.root/'skill'
        shutil.copytree(self.app/'Contents/Resources/skills/lulu-agent', self.skill)
        self.verifiers = [
            load(ROOT/'agent-skill/lulu-agent/scripts/verify_lulu.py'),
            load(ROOT/'app/skills/lulu-agent/scripts/verify_lulu_generation9.py'),
        ]

    def verify(self, verifier):
        kwargs = {} if verifier is self.verifiers[0] else dict(launcher=self.launcher, installed_skill=self.skill)
        with patch.object(verifier, 'APP', self.app):
            return verifier.verify(self.app, state_root=self.state, **kwargs)

    def reject(self):
        for verifier in self.verifiers:
            with self.subTest(verifier=verifier.__file__), self.assertRaises((OSError, RuntimeError, ValueError)):
                self.verify(verifier)

    def change_json(self, relative, mutate):
        p = self.state/relative
        value = json.loads(p.read_bytes())
        mutate(value)
        p.write_text(json.dumps(value))


    def prepare_local(self):
        from test_local_task_migration import LocalTaskMigrationTests
        from src.core.agent_entry.local_task_storage import prepare_local_app
        LocalTaskMigrationTests.set_version(self, '4.2.1', '20261013.3')
        self.state = prepare_local_app(self.app, self.product)
        shutil.rmtree(self.skill)
        shutil.copytree(self.app/'Contents/Resources/skills/lulu-agent', self.skill)


















