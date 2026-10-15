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
