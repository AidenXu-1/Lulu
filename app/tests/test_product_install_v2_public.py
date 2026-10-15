import json
from pathlib import Path
import shutil
from src.core.agent_entry.product_install import ProductInstaller, ProductPaths
from src.core.agent_entry.ownership import verify_ownership_receipt, verify_ready_action_receipt
import unittest
import test_delivery_package_v2_public as packages
from test_generation9_formal_install_public import MemoryKeys, MemoryService


class ProductInstallV2PublicTests(unittest.TestCase):
    command = packages.DeliveryPackageV2PublicTests.command
    def setUp(self):
        packages.DeliveryPackageV2PublicTests.setUp(self)
        self.home=self.root/'home';self.home.mkdir()
        self.paths=ProductPaths(self.root/'Applications/Lulu.app',self.home/'state',
            self.home/'LaunchAgents/core.plist',self.home/'skills/lulu-agent',self.home/'bin/lulu')
        self.keys=MemoryKeys();self.service=MemoryService()
        self.installer=ProductInstaller(self.paths,key_store=self.keys,service=self.service,
            stop_clients=lambda:None,clients_stopped=lambda:True)

    def install_product(self):
        self.expect_stage=True
        return self.installer.install_from_dmg(self.dmg,source_app=self.app,
            expected_sha256=self.expected,stage_root=self.stage,transaction_id='install-first-0001')

    def test_exact_same_delivery_repairs_damaged_app_without_reinterpreting_tasks(self):
        from unittest.mock import Mock
        from src.core.agent_entry.task_handover import FILENAME
        from src.core.agent_entry.installed_materials import MATERIALS_DIR
        self.installer.task_exporter = Mock(side_effect=AssertionError("no damaged app execution"))
        self.install_product()
        old_handover = (self.paths.state_root / MATERIALS_DIR / FILENAME).read_bytes()
        jobs = self.paths.state_root.parent / "jobs"
        jobs.mkdir(); (jobs / "unchanged-ledger").write_bytes(b"existing task bytes")
        extra = self.paths.app_bundle / "Contents/Resources/extra-cache.pyc"
        extra.write_bytes(b"untracked generated cache")
        with self.assertRaisesRegex(RuntimeError, "COMPONENT_MISMATCH"):
            self.installer.verify_installed()
        self.stage = self.root / "repair-stage"
        self.installer.install_from_dmg(self.dmg, source_app=self.app,
            expected_sha256=self.expected, stage_root=self.stage, transaction_id="same-repair-0002")
        self.installer.verify_installed()
        self.assertFalse(extra.exists())
        self.assertEqual((jobs / "unchanged-ledger").read_bytes(), b"existing task bytes")
        repaired = json.loads((self.paths.state_root / MATERIALS_DIR / FILENAME).read_bytes())
        self.assertEqual(repaired['body']['jobs'], json.loads(old_handover)['body']['jobs'])
        self.installer.task_exporter.assert_not_called()
        backups = list(self.paths.app_bundle.parent.glob(".Lulu.app.lulu-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "Contents/Resources/extra-cache.pyc").read_bytes(), b"untracked generated cache")
