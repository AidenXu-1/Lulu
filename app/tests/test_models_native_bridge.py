"""End-to-end page wiring, without installed data, Keychain or remote requests."""
import json,os,subprocess,sys,unittest
from pathlib import Path
from types import SimpleNamespace
class ModelsNativeBridgeTests(unittest.TestCase):
 def test_model_routes_use_registered_purpose_for_all_models(self):
  from src.ui.model_settings.widget import ModelsPage
  from src.ui.model_settings.controller import MODEL_IDS
  from src.core.engines.local_models import LOCAL_MODELS
  for display, model_id in MODEL_IDS.items():
   with self.subTest(model=model_id):
    sent=[]
    ModelsPage.show_model(SimpleNamespace(send=lambda *args:sent.append(args)),model_id)
    self.assertEqual(sent,[('purpose',dict(purpose='extract' if model_id in LOCAL_MODELS else 'voice',
                                         localMode='local',choice=display))])
  with self.assertRaises(ValueError):
   ModelsPage.show_model(SimpleNamespace(send=lambda *args:None),'unknown')

 def test_real_page_renders_core_state_and_keeps_navigation_local(self):
  env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
  result=subprocess.run([sys.executable,str(Path(__file__).with_name('model_settings_native_child.py'))],env=env,capture_output=True,text=True,timeout=60)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  report=json.loads(result.stdout.strip().splitlines()[-1])
  self.assertEqual(report['status'],'passed');self.assertEqual(report['passiveSecretReads'],0)
  self.assertTrue(report['credentialRemoval'])
