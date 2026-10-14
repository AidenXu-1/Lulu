import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from test_delivery_package_v2_public import signed_delivery
from src.core.agent_entry.delivery_identity import delivery_app_tree_identity

ROOT=Path(__file__).resolve().parents[1]

class DeliveryPackagingTests(unittest.TestCase):
    def test_identity_consumers_agree_on_final_signed_v2_and_reject_profile_drift(self):
        profile=runpy.run_path(str(ROOT/'packaging/profile_gate.py'))
        verify=runpy.run_path(str(ROOT/'packaging/verify_generation9_candidate_identity.py'))['verify_candidate_identity']
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();app=signed_delivery(root);identity=delivery_app_tree_identity(app)
            self.assertEqual(profile['candidate_app_tree_identity'](app,'external_candidate'),identity)
            receipt={'ok':True,'digests':{'app_tree_sha256':identity['sha256']},'app_tree':identity,
                'app_tree_binding':{'stage':'final_signed_v2','first_recomputation':identity['sha256'],'second_recomputation':identity['sha256']},
                'bundle_seal':{'direct_codesign_returncode':0,'strict_returncode':0}}
            path=root/'profile.json';path.write_text(json.dumps(receipt))
            self.assertTrue(verify(app=app,profile_receipt_path=path)['ok'])
            receipt['bundle_seal']['strict_returncode']=1;path.write_text(json.dumps(receipt))
            with self.assertRaises(RuntimeError):verify(app=app,profile_receipt_path=path)

    def test_standalone_tree_spec_matches_normative_protocol_bytes(self):
        self.assertEqual((ROOT/'src/core/agent_entry/delivery_identity.py').read_bytes(),
            (ROOT/'skills/lulu-agent/scripts/verify_delivery_tree_v2.py').read_bytes())

    def test_profile_public_writer_persists_the_same_v2_binding_it_returns(self):
        import test_build_profiles as fixtures
        from src.core.agent_entry.delivery_identity import prepare_delivery_marker,prepare_link_policy
        case=fixtures.BuildProfileTests()
        profile=fixtures.PROFILE_GATE
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();app,analysis=case._synthetic_external_app(root)
            resources=app/'Contents/Resources'
            prepare_delivery_marker(app);prepare_link_policy(app);case._resign(app)
            data=json.loads(analysis.read_bytes())
            profile['write_analysis_receipt'](analysis,'external_candidate',
                [('src.main',str(ROOT/'src/main.py'))],
                [*data['resources'],'component_delivery.v2.json'],data['profile_sha256'])
            output=root/'profile.json'
            returned=profile['_verify_app_with_reader'](app,'external_candidate',analysis,output,
                case._synthetic_archive_reader,expected_product_source=json.loads((resources/'product_source.json').read_bytes()))
            persisted=json.loads(output.read_bytes())
            self.assertEqual(persisted,returned)
            self.assertEqual(persisted['app_tree_binding']['stage'],'final_signed_v2')
            self.assertEqual(persisted['bundle_seal']['strict_returncode'],0)
