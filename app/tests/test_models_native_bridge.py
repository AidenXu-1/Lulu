"""End-to-end page wiring, without installed data, Keychain or remote requests."""
import json,os,subprocess,sys,unittest
from pathlib import Path
class ModelsNativeBridgeTests(unittest.TestCase):
 def test_real_page_renders_core_state_and_keeps_navigation_local(self):
  env=dict(os.environ,PYTHONPATH=str(Path(__file__).parents[1]))
  result=subprocess.run([sys.executable,str(Path(__file__).with_name('model_settings_native_child.py'))],env=env,capture_output=True,text=True,timeout=60)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  report=json.loads(result.stdout.strip().splitlines()[-1])
  self.assertEqual(report['status'],'passed');self.assertEqual(report['passiveSecretReads'],0)
  self.assertTrue(report['credentialRemoval'])
