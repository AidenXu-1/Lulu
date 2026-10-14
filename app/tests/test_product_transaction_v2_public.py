"""Multi-path product replacement interrupted at real rename/commit boundaries."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.agent_entry.product_transaction import ProductTransaction, ProductCrash, assert_business_available
from test_generation9_formal_install_public import MemoryService


class ProductTransactionTests(unittest.TestCase):
    def fixture(self,root,old):
        state=root/'state';state.mkdir()
        targets={name:root/'installed'/name for name in ('app','materials','core_registration','ownership')}
        sources={name:root/'staged'/name for name in targets}
        for name,path in sources.items():
            path.parent.mkdir(exist_ok=True);path.write_text('new:'+name)
        if old:
            for name,path in targets.items():
                path.parent.mkdir(exist_ok=True);path.write_text('old:'+name)
            (state/'current.json').write_text('{"old":"exact bytes"}\n');(state/'current.json').chmod(0o600)
        service=MemoryService(); service.loaded=old
        return state,targets,sources,service

    def execute(self,tx,sources,**kwargs):
        return tx.execute(sources,transaction_id='transaction-0001',binding={
            'release_dmg_sha256':'1'*64,'component_manifest_sha256':'2'*64,
            'app_component_set_digest':'3'*64,'installation_id_digest':'4'*64,'candidate_generation':9},
            stop_clients=lambda:None,clients_stopped=lambda:True,final_verifier=lambda:True,**kwargs)

    def test_first_install_without_optional_skill_commits_then_allows_business(self):
        with TemporaryDirectory() as tmp:
            state,targets,sources,service=self.fixture(Path(tmp).resolve(),False)
            tx=ProductTransaction(state,targets,service=service)
            proof=self.execute(tx,sources)
            self.assertEqual(proof['state'],'ready');self.assertTrue(service.loaded)
            self.assertEqual(proof['action'],'product_install')
            assert_business_available(state)
            self.assertFalse((Path(tmp)/'installed/skill').exists())

    def test_every_switch_and_commit_interruption_blocks_business_then_restores_exact_old_state(self):
        cuts=['prepared','clients_stopped']
        for name in ('app','materials','core_registration','ownership'):
            cuts.extend((name+':intent',name+':backup',name+':effect',name+':applied'))
        cuts.extend(('service_started','final_verified','current_written','committed'))
        for old in (False,True):
            for cut in cuts:
                with self.subTest(old=old,cut=cut),TemporaryDirectory() as tmp:
                    state,targets,sources,service=self.fixture(Path(tmp).resolve(),old)
                    tx=ProductTransaction(state,targets,service=service)
                    with self.assertRaises(ProductCrash):self.execute(tx,sources,crash_after=cut)
                    if cut!='committed':
                        with self.assertRaises(RuntimeError):assert_business_available(state)
                    result=ProductTransaction(state,targets,service=service).recover(
                        'transaction-0001',stop_clients=lambda:None,clients_stopped=lambda:True,
                        rollback_verifier=lambda:True,final_verifier=lambda:True)
                    if cut=='committed':
                        self.assertEqual(result['state'],'ready')
                        for name,path in targets.items():self.assertEqual(path.read_text(),'new:'+name)
                    else:
                        self.assertEqual(result['state'],'restored_original_state')
                        for name,path in targets.items():
                            if old:self.assertEqual(path.read_text(),'old:'+name)
                            else:self.assertFalse(path.exists())
                        self.assertEqual(service.loaded,old)
                        if old:self.assertEqual((state/'current.json').read_bytes(),b'{"old":"exact bytes"}\n')
                        else:self.assertFalse((state/'current.json').exists())
                    assert_business_available(state)

    def test_clients_still_running_causes_zero_target_changes(self):
        with TemporaryDirectory() as tmp:
            state,targets,sources,service=self.fixture(Path(tmp).resolve(),True)
            tx=ProductTransaction(state,targets,service=service)
            with self.assertRaisesRegex(RuntimeError,'PRODUCT_CLIENTS_STILL_RUNNING'):
                tx.execute(sources,transaction_id='transaction-0001',binding={
                    'release_dmg_sha256':'1'*64,'component_manifest_sha256':'2'*64,
                    'app_component_set_digest':'3'*64,'installation_id_digest':'4'*64,'candidate_generation':9},
                    stop_clients=lambda:None,clients_stopped=lambda:False,final_verifier=lambda:True)
            for name,path in targets.items():self.assertEqual(path.read_text(),'old:'+name)

    def test_recovery_preserves_unexpected_content_and_keeps_business_closed(self):
        with TemporaryDirectory() as tmp:
            state,targets,sources,service=self.fixture(Path(tmp).resolve(),True)
            tx=ProductTransaction(state,targets,service=service)
            with self.assertRaises(ProductCrash):self.execute(tx,sources,crash_after='app:effect')
            targets['app'].write_text('unexpected content')
            with self.assertRaises(RuntimeError):tx.recover('transaction-0001',stop_clients=lambda:None,
                clients_stopped=lambda:True,rollback_verifier=lambda:True,final_verifier=lambda:True)
            self.assertEqual(targets['app'].read_text(),'unexpected content')
            with self.assertRaises(RuntimeError):assert_business_available(state)

    def test_committed_service_drift_never_becomes_rollback_on_repeated_recovery(self):
        with TemporaryDirectory() as tmp:
            state,targets,sources,service=self.fixture(Path(tmp).resolve(),True)
            tx=ProductTransaction(state,targets,service=service)
            self.execute(tx,sources);service.stop()
            for _ in range(2):
                with self.assertRaises(RuntimeError):tx.recover('transaction-0001',stop_clients=lambda:None,
                    clients_stopped=lambda:True,rollback_verifier=lambda:True,final_verifier=lambda:True)
                for name,path in targets.items():self.assertEqual(path.read_text(),'new:'+name)
                with self.assertRaises(RuntimeError):assert_business_available(state)

    def test_recovery_preserves_unexpected_current_receipt_before_any_target_moves(self):
        with TemporaryDirectory() as tmp:
            state,targets,sources,service=self.fixture(Path(tmp).resolve(),True)
            tx=ProductTransaction(state,targets,service=service)
            with self.assertRaises(ProductCrash):self.execute(tx,sources,crash_after='app:effect')
            current=state/'current.json';current.write_text('third party receipt')
            with self.assertRaises(RuntimeError):tx.recover('transaction-0001',stop_clients=lambda:None,
                clients_stopped=lambda:True,rollback_verifier=lambda:True,final_verifier=lambda:True)
            self.assertEqual(current.read_text(),'third party receipt')
            self.assertEqual(targets['app'].read_text(),'new:app')
            with self.assertRaises(RuntimeError):assert_business_available(state)
