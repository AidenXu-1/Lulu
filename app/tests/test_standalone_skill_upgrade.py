"""Optional Skill upgrades across local-storage generations, using signed tiny Apps only."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from src.core.agent_entry import standalone_skill
from src.core.agent_entry.local_task_storage import prepare_local_app
from src.core.agent_entry.standalone_state import verify_install_state
from src.core.local_integrity import product_local_store
from tests import test_local_task_migration as migration_fixture
from tests import test_standalone_skill_verifier as skill_fixture


class LocalOptionalSkillUpgradeTests(unittest.TestCase):
    def setUp(self):
        skill_fixture.StandaloneSkillVerifierTests.setUp(self)
        skill_fixture.StandaloneSkillVerifierTests.prepare_local(self)
        self.keys = product_local_store(self.product)
        self.target = self.root/'managed-skill'
        self.shortcut = self.root/'managed-lulu'
        self.old_state = self.state
        self.old_installation = self.installation(self.state)
        standalone_skill.install(self.old_installation, transaction_id='old-skill')
        self.receipt = (self.old_state/'optional/ownership.json').read_bytes()
        migration_fixture.LocalTaskMigrationTests.set_version(self, '4.3.1', '20261021.6')
        self.state = prepare_local_app(self.app, self.product)
        self.current = self.installation(self.state)

    def installation(self, state):
        return SimpleNamespace(keys=self.keys, paths=SimpleNamespace(app_bundle=self.app,
            state_root=state, skill_target=self.target, launcher_target=self.shortcut),
            verify_installed=lambda: verify_install_state(self.app, state_root=state, keys=self.keys))

    def test_local_generation_finds_authenticated_previous_skill_and_updates_exact_files(self):
        self.assertEqual(standalone_skill.status(self.current), 'update_available')
        result = standalone_skill.install(self.current, transaction_id='new-skill')
        self.assertEqual((result['phase'], result['state']), ('COMMITTED', 'ready'))
        self.assertEqual(standalone_skill.status(self.current), 'ready')
        manifest = json.loads((self.target/'manifest.json').read_text())
        self.assertEqual(manifest['product_identity']['product_build'], '20261021.6')
        self.assertEqual((self.old_state/'optional/ownership.json').read_bytes(), self.receipt)

    def test_modified_previous_skill_is_not_overwritten(self):
        path = self.target/'SKILL.md'
        path.write_text(path.read_text()+'\nUser-owned edit.\n')
        before = path.read_bytes()
        self.assertEqual(standalone_skill.status(self.current), 'ownership_conflict')
        with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_OWNERSHIP_CONFLICT'):
            standalone_skill.install(self.current, transaction_id='modified-skill')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.state/'optional/current.json').exists())

    def test_another_installation_cannot_authorize_replacement(self):
        owner, _ = self.current.verify_installed()
        owner = dict(owner, installation_id='unrelated-installation',
            installation_id_digest=hashlib.sha256(b'unrelated-installation').hexdigest())
        with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_OWNERSHIP_CONFLICT'):
            standalone_skill.owned_digests(self.current, owner)

    def test_symlinked_previous_generation_cannot_authorize_replacement(self):
        moved = self.root/'moved-old-generation'
        self.old_state.rename(moved)
        self.old_state.symlink_to(moved, target_is_directory=True)
        self.assertEqual(standalone_skill.status(self.current), 'ownership_conflict')
        with self.assertRaisesRegex(RuntimeError, 'AGENT_ENTRY_OWNERSHIP_CONFLICT'):
            standalone_skill.install(self.current, transaction_id='linked-skill')
