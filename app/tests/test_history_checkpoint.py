"""Upgrade checkpoints use verified old-Core projections, never old private keys."""
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch, Mock


class Keys:
    def get_text(self, name):
        return base64.b64encode(b'x'*32).decode()


class HistoryCheckpointTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.state=self.root/'AgentEntry'
        self.generation='b'*40;self.keys=Keys()
        self.owner={'installation_id':'fixture-install','installation_id_digest':hashlib.sha256(b'fixture-install').hexdigest(),'component_manifest_sha256':'a'*64}
        self.taskroot=self.root/'task-generations'/self.generation
        self.taskroot.mkdir(parents=True);(self.taskroot/'state').write_text('original')
        self.rows=[{'job_id':'fixture-1','job_kind':'link_extraction','job_revision':2,'state':'failed','lease_epoch':0,'last_sequence':2,'result_ref':None,'receipt_ref':'lulu-job:fixture-1:revision:2','partial_failures':[]}]
        self.context={'tasks':{},'view':{'groups':[],'hidden_ids':[]}}

    def write(self):
        from src.core.agent_entry.history_checkpoint import write_checkpoint
        return write_checkpoint(product_root=self.root,owner=self.owner,generation=self.generation,keys=self.keys,records=self.rows,context=self.context,expected_snapshot=self.snapshot(),state=self.state)

    def snapshot(self):
        from src.core.agent_entry.product_transaction import snapshot_digest
        return snapshot_digest(self.taskroot)

    def read(self):
        from src.core.agent_entry.history_checkpoint import read_checkpoint
        return read_checkpoint(product_root=self.root,owner=self.owner,generation=self.generation,keys=self.keys,state=self.state)


    def producer(self, *, changed=False, signature_error=False):
        from contextlib import ExitStack
        from src.core.agent_entry import history_checkpoint as module
        app=self.root/'Lulu.app'
        def cli(args):
            if args[1]=='list':return {'jobs':[{k:v for k,v in self.rows[0].items() if k!='partial_failures'}]}
            if changed:(self.taskroot/'state').write_text('new')
            return self.rows[0]
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path,'home',return_value=self.root/'home'))
            # Match the installer's canonical product root with this fixture.
            product=self.root/'home/Library/Application Support/Lulu';product.parent.mkdir(parents=True,exist_ok=True)
            self.taskroot.rename(self.root/'saved-generation')
            self.taskroot=product/'task-generations'/self.generation;self.taskroot.parent.mkdir(parents=True)
            (self.root/'saved-generation').rename(self.taskroot)
            self.root=product;self.state=product/'AgentEntry'
            stack.enter_context(patch('src.core.agent_entry.standalone_paths.installed_app',return_value=app))
            stack.enter_context(patch('src.core.agent_entry.standalone_paths.state_for_app',return_value=self.state))
            stack.enter_context(patch('src.core.agent_entry.standalone_source.core_cdhash',return_value=self.generation))
            owner_check=stack.enter_context(patch('src.core.agent_entry.ownership.verify_ownership_receipt',return_value=self.owner))
            stack.enter_context(patch('src.core.agent_entry.ownership.verify_ready_action_receipt'))
            seal=stack.enter_context(patch('src.core.agent_entry.component_manifest.verify_component_manifest',return_value={'component_manifest_sha256':self.owner['component_manifest_sha256']},side_effect=RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH') if signature_error else None))
            stack.enter_context(patch('src.core.agent_entry.task_handover_context.capture_context',return_value=self.context))
            result=module.prepare_checkpoint(app,keys=self.keys,run_cli=cli)
            self.assertEqual(seal.call_count,2);self.assertEqual(owner_check.call_count,2)
            return result


    def test_producer_carries_completed_live_recording_without_fabricating_result(self):
        self.rows[0].update(job_kind='live_recording', state='succeeded')
        self.producer()
        self.assertEqual(self.read(), (self.rows, self.context))
        self.assertIsNone(self.read()[0][0]['result_ref'])
