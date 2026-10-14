from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.agent_entry.ownership import create_ownership_receipt, replace_ownership_receipt, verify_ownership_receipt
from test_generation9_formal_install_public import MemoryKeys


class ProductOwnershipTests(unittest.TestCase):
    def test_base_product_then_optional_skill_retains_one_installation(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'ownership.json';keys=MemoryKeys()
            base=create_ownership_receipt(path,key_store=keys,release_dmg_sha256='1'*64,
                component_manifest_sha256='2'*64,app_component_set_digest='3'*64,
                installation_id='one-installation',candidate_generation=9,
                managed_effect_digests={'core_registration':'4'*64},product_scope=True)
            self.assertEqual(base['schema_id'],'lulu.agent-entry-ownership/v3')
            self.assertEqual(verify_ownership_receipt(path,key_store=keys),base)
            added=replace_ownership_receipt(path,key_store=keys,previous=base,
                release_dmg_sha256='1'*64,component_manifest_sha256='2'*64,
                app_component_set_digest='3'*64,candidate_generation=9,
                managed_effect_digests={'core_registration':'4'*64,'skill':'5'*64,'launcher':'6'*64},product_scope=True)
            self.assertEqual(added['installation_id'],base['installation_id'])
            self.assertEqual(len(keys.values),1)
            self.assertEqual(verify_ownership_receipt(path,key_store=keys),added)

    def test_partial_optional_pair_and_legacy_core_only_are_forbidden(self):
        for effects,product_scope in (({'core_registration':'4'*64},False),
            ({'core_registration':'4'*64,'skill':'5'*64},True),({'launcher':'6'*64},True)):
            with self.subTest(effects=effects),TemporaryDirectory() as tmp:
                keys=MemoryKeys();path=Path(tmp)/'ownership.json'
                with self.assertRaises(RuntimeError):
                    create_ownership_receipt(path,key_store=keys,release_dmg_sha256='1'*64,
                        component_manifest_sha256='2'*64,app_component_set_digest='3'*64,
                        installation_id='one-installation',candidate_generation=9,
                        managed_effect_digests=effects,product_scope=product_scope)
                self.assertEqual(keys.values,{})
                self.assertFalse(path.exists())
