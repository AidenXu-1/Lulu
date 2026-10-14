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

    def test_base_install_is_ready_without_skill_and_reuses_same_product_for_optional_install(self):
        from src.core.agent_entry.installed_materials import component_manifest_path
        result=self.install_product()
        owner=verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)
        verify_ready_action_receipt(self.paths.state_root/'current.json',ownership=owner)
        self.assertEqual(set(owner['managed_effect_digests']),{'core_registration'})
        self.assertFalse(self.paths.skill_target.exists());self.assertFalse(self.paths.launcher_target.exists())
        self.assertEqual(self.installer.skill_status(),'not_installed')
        self.assertTrue(component_manifest_path(self.paths.app_bundle,state_root=self.paths.state_root).is_file())
        self.installer.install_skill(transaction_id='install-skill-0002')
        added=verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)
        self.assertEqual(added['installation_id'],owner['installation_id'])
        self.assertEqual(self.installer.skill_status(),'ready')
        self.assertEqual(self.service.starts,1)

    def test_missing_dmg_old_mixed_install_migrates_without_rewriting_old_receipts(self):
        import test_generation9_formal_install_public as legacy
        from src.core.agent_entry.formal_install import FormalAgentEntryInstaller,FormalInstallPaths
        old=legacy._fixture_app(self.root/'old')
        dmg,receipt=legacy._verified_dmg_receipt(self.root/'legacy-dmg',old)
        self.paths.app_bundle.parent.mkdir()
        shutil.copytree(old,self.paths.app_bundle)
        formal=FormalAgentEntryInstaller(FormalInstallPaths(app_bundle=self.paths.app_bundle,
            dmg=dmg,dmg_receipt=receipt,state_root=self.paths.state_root,
            launch_agent_target=self.paths.launch_agent_target,skill_target=self.paths.skill_target,
            launcher_target=self.paths.launcher_target),key_store=self.keys,service=self.service,
            installation_id_factory=lambda:'same-original-installation')
        formal.install(transaction_id='legacy-first-0000')
        old_owner=(self.paths.state_root/'ownership.json').read_bytes()
        # The App was updated while the old managed receipt and Skill remained.
        shutil.rmtree(self.paths.app_bundle)
        newer=legacy._fixture_app(self.root/'newer',product_build='20260905.1')
        shutil.copytree(newer,self.paths.app_bundle);dmg.unlink()
        self.installer.legacy_reference_app=old
        self.install_product()
        now=verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)
        self.assertEqual(now['installation_id'],'same-original-installation')
        self.assertEqual(self.installer.skill_status(),'ready')
        backups=list(self.paths.state_root.glob('.ownership.json.lulu-backup-*'))
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),old_owner)

    def test_fresh_product_interruption_restores_absence_and_removes_only_new_key(self):
        from src.core.agent_entry.product_transaction import ProductCrash
        for cut in ('prepared','app:effect','materials:effect','ownership:effect','final_verified','current_written'):
            with self.subTest(cut=cut):
                if self.stage.exists():shutil.rmtree(self.stage)
                tid='fresh-'+cut.replace(':','-')+'-0003'
                with self.assertRaises(ProductCrash):self.installer.install_from_dmg(self.dmg,
                    source_app=self.app,expected_sha256=self.expected,stage_root=self.stage,
                    transaction_id=tid,crash_after=cut)
                self.installer.recover_product(tid)
                self.assertFalse(self.paths.app_bundle.exists())
                self.assertFalse((self.paths.state_root/'ownership.json').exists())
                self.assertFalse((self.paths.state_root/'current.json').exists())
                self.assertEqual(self.keys.values,{})

    def test_optional_skill_interruption_restores_base_without_core_restart(self):
        from src.core.agent_entry.transaction import CrashInjected
        self.install_product()
        owner=(self.paths.state_root/'ownership.json').read_bytes()
        current=(self.paths.state_root/'current.json').read_bytes()
        for cut in ('skill:intent','skill:effect','launcher:effect','ownership:effect'):
            with self.subTest(cut=cut):
                tid='skill-'+cut.replace(':','-')+'-0004'
                with self.assertRaises(CrashInjected):self.installer.install_skill(transaction_id=tid,crash_after=cut)
                self.installer.recover_skill(transaction_id=tid)
                self.assertEqual((self.paths.state_root/'ownership.json').read_bytes(),owner)
                self.assertEqual((self.paths.state_root/'current.json').read_bytes(),current)
                self.assertEqual(self.installer.skill_status(),'not_installed')
                self.assertEqual(self.service.starts,1)

    def test_base_gui_task_store_works_without_skill_and_detects_later_material_drift(self):
        from unittest.mock import patch
        from src.core.agent_entry import requirements
        from src.core.agent_entry.installed_materials import component_manifest_path
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        from src.core.gui_task_ledger import CoreTaskQueueStore
        from src.core.task import Task
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.job_ledger.service import JobControlService
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
        from test_shared_core_gui_task_ledger import _runtime
        self.install_product()
        resolve=lambda app:component_manifest_path(app,state_root=self.paths.state_root)
        with patch.object(requirements,'component_manifest_path',side_effect=resolve):
            gui,core,proof=requirements.load_gui_peer_requirements(self.paths.app_bundle,audit_session_id=123)
        self.assertEqual(proof['state'],'ready')
        ledger=JobLedgerStore(self.root.resolve()/'jobs',FakeIntegrityAnchorStore())
        runtime=_runtime(JobControlService(ledger),('gui-job-001','gui-attempt-001','gui-lineage-001'))
        dispatcher=CoreCommandDispatcher(product_identity={'adapter_id':'agent_cli'},
            resource_root=self.paths.app_bundle/'Contents/Resources',runtime_factory=lambda:runtime)
        with patch('src.core.agent_entry.product_install.build_default_product_installer',return_value=self.installer):
            queue=CoreTaskQueueStore(lambda command,arguments:dispatcher({'command':command,'arguments':arguments}))
            task=Task(id=1,source='/tmp/offline.wav',kind='file',title='独立GUI任务')
            queue.register(task)
            self.assertEqual(len(queue.load().tasks),1)
            self.assertFalse(self.paths.skill_target.exists());self.assertFalse(self.paths.launcher_target.exists())
            manifest=resolve(self.paths.app_bundle);manifest.write_bytes(manifest.read_bytes()+b' ')
            with self.assertRaises(RuntimeError):queue.load()

    def test_standalone_skill_verifier_accepts_same_v2_and_rejects_material_drift(self):
        from test_generation9_skill_verifier import _load_verifier
        self.install_product();self.installer.install_skill(transaction_id='skill-verifier-0005')
        verifier=_load_verifier();verifier.APP=self.paths.app_bundle
        proof=verifier.verify(self.paths.app_bundle,launcher=self.paths.launcher_target,
            installed_skill=self.paths.skill_target,state_root=self.paths.state_root)
        self.assertTrue(proof['verified'])
        path=self.paths.state_root/'product-materials-v2/Lulu.components.v2.json'
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(RuntimeError):verifier.verify(self.paths.app_bundle,launcher=self.paths.launcher_target,
            installed_skill=self.paths.skill_target,state_root=self.paths.state_root)

    def test_next_installer_run_recovers_key_intent_before_first_journal(self):
        from unittest.mock import patch
        class PowerLoss(BaseException):pass
        original=self.keys.write_new
        def stop_after_key(name,value):
            original(name,value);raise PowerLoss()
        with patch.object(self.keys,'write_new',side_effect=stop_after_key):
            with self.assertRaises(PowerLoss):self.install_product()
        self.assertEqual(len(self.keys.values),1)
        self.installer.recover_pending()
        self.assertEqual(self.keys.values,{})
        self.assertFalse(self.paths.app_bundle.exists())
