import json
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
from src.core.agent_entry import product_lifecycle as lifecycle,product_install as product
from test_product_install_v2_public import ProductInstallV2PublicTests

class QuitOutcomeTests(unittest.TestCase):
 def request(self,accepted,states,*,instance=3):
  objc=SimpleNamespace(objc_getClass=Mock(return_value=1),sel_registerName=Mock(return_value=2),objc_msgSend=Mock())
  lookup=Mock(return_value=instance);terminate=Mock(return_value=accepted)
  with patch.object(lifecycle.ctypes,'CDLL',side_effect=[object(),objc]),patch.object(lifecycle.ctypes,'cast',return_value=SimpleNamespace(value=1)),patch.object(lifecycle.ctypes,'CFUNCTYPE',side_effect=[lambda address:lookup,lambda address:terminate]),patch.object(lifecycle,'product_processes',side_effect=states),patch.object(lifecycle.time,'monotonic',side_effect=[0,31]):
   lifecycle.stop_clients()
  terminate.assert_called_once()
 def test_false_but_actual_exit_succeeds(self):self.request(False,[{12345:'Lulu'},{},{}])
 def test_false_and_still_running_refuses(self):
  with self.assertRaisesRegex(RuntimeError,'PRODUCT_CLIENTS_STILL_RUNNING'):self.request(False,[{12345:'Lulu'}]*3)
 def test_true_and_still_running_refuses(self):
  with self.assertRaisesRegex(RuntimeError,'PRODUCT_CLIENTS_STILL_RUNNING'):self.request(True,[{12345:'Lulu'}]*3)
 def test_missing_gui_instance_refuses(self):
  with self.assertRaisesRegex(RuntimeError,'PRODUCT_GUI_QUIT_REQUIRED'):self.request(False,[{12345:'Lulu'}],instance=0)

class FailureEvidenceTests(unittest.TestCase):
 command=ProductInstallV2PublicTests.command
 setUp=ProductInstallV2PublicTests.setUp
 install_product=ProductInstallV2PublicTests.install_product
 def fail_once(self,error):
  calls=[]
  def stop():
   calls.append(1)
   if len(calls)==1:raise error
  self.installer.stop_clients=stop
 def assert_restored(self):
  self.assertEqual(json.loads((self.paths.state_root/'product-installation.json').read_text())['phase'],'ROLLED_BACK')
  self.assertFalse(self.paths.app_bundle.exists())
  self.assertEqual(self.keys.values,{})
 def failure_file(self):return self.paths.state_root/'product-transactions/install-first-0001/failure.json'
 def test_original_failure_retains_pre_recovery_phase(self):
  error=RuntimeError('PRODUCT_GUI_QUIT_REQUIRED');self.fail_once(error)
  with self.assertRaises(RuntimeError) as cm:self.install_product()
  self.assertIs(cm.exception,error);self.assert_restored()
  value=json.loads(self.failure_file().read_text())
  self.assertEqual(value,{'schema_id':'lulu.product-install-failure/v1','phase':'PREPARED','effect_count':0,'error_code':'PRODUCT_GUI_QUIT_REQUIRED','error_type':'RuntimeError','errno':None,'operation_stage':'stop_clients','target':None})
  self.assertEqual(self.failure_file().stat().st_mode&0o777,0o600)
 def test_unknown_message_and_private_path_never_recorded(self):
  error=OSError(5,'private password=value /Users/private/file');self.fail_once(error)
  with self.assertRaises(OSError) as cm:self.install_product()
  self.assertIs(cm.exception,error);self.assert_restored()
  raw=self.failure_file().read_text();value=json.loads(raw)
  self.assertEqual(value['error_code'],'UNKNOWN');self.assertEqual(value['errno'],5)
  for secret in ('private','password','/Users/','value'):self.assertNotIn(secret,raw)
 def test_service_drift_code_survives_recovery(self):
  self.fail_once(RuntimeError('AGENT_ENTRY_TRANSACTION_DRIFT'))
  with self.assertRaisesRegex(RuntimeError,'AGENT_ENTRY_TRANSACTION_DRIFT'):self.install_product()
  self.assert_restored()
  self.assertEqual(json.loads(self.failure_file().read_text())['error_code'],'AGENT_ENTRY_TRANSACTION_DRIFT')
 def test_diagnostic_write_failure_does_not_block_recovery(self):
  error=RuntimeError('PRODUCT_GUI_QUIT_REQUIRED');self.fail_once(error)
  with patch.object(product,'_record_install_failure',side_effect=OSError(28,'full'),create=True) as record:
   with self.assertRaises(RuntimeError) as cm:self.install_product()
  self.assertIs(cm.exception,error);self.assert_restored();record.assert_called_once()
 def test_same_oserror_identifies_prepared_call_site(self):
  from contextlib import ExitStack
  from src.core.agent_entry import product_transaction as transaction
  # Each iteration owns a fresh fixture and the same opaque error.
  for point in ('stop_clients','stop_core','verify_target_before','save_target_intent'):
   with self.subTest(point=point):
    case=FailureEvidenceTests();case.setUp()
    try:
     error=OSError(5,'private identical text');armed=[False];fired=[False]
     def stop():
      armed[0]=True
      if point=='stop_clients' and not fired[0]:fired[0]=True;raise error
      if point=='stop_core' and not fired[0]:case.service.loaded=True
     case.installer.stop_clients=stop
     original_stop=case.service.stop;original_digest=transaction.snapshot_digest;original_save=transaction.ProductTransaction._save
     def stop_core():
      if not fired[0]:fired[0]=True;raise error
      original_stop()
     def digest(path):
      if point=='verify_target_before' and armed[0] and not fired[0] and path==case.paths.app_bundle:
       fired[0]=True;raise error
      return original_digest(path)
     def save(tx,journal,phase):
      if point=='save_target_intent' and phase=='APP_INTENT' and not fired[0]:fired[0]=True;raise error
      return original_save(tx,journal,phase)
     with ExitStack() as stack:
      if point=='stop_core':stack.enter_context(patch.object(case.service,'stop',side_effect=stop_core))
      stack.enter_context(patch.object(transaction,'snapshot_digest',side_effect=digest))
      stack.enter_context(patch.object(transaction.ProductTransaction,'_save',new=save))
      with self.assertRaises(OSError) as cm:case.install_product()
     self.assertIs(cm.exception,error);case.assert_restored()
     value=json.loads(case.failure_file().read_text())
     self.assertEqual(value['phase'],'PREPARED');self.assertEqual(value['error_code'],'UNKNOWN')
     self.assertEqual(value['operation_stage'],point)
     self.assertEqual(value['target'],'app' if point in ('verify_target_before','save_target_intent') else None)
    finally:case.doCleanups()
 def test_original_evidence_exists_before_recovery_can_fail(self):
  original=RuntimeError('PRODUCT_GUI_QUIT_REQUIRED');self.fail_once(original)
  recovery=RuntimeError('PRODUCT_RECOVERY_DRIFT')
  def fail_recovery(tid):
   self.assertEqual(json.loads(self.failure_file().read_text())['error_code'],'PRODUCT_GUI_QUIT_REQUIRED')
   raise recovery
  with patch.object(self.installer,'recover_product',side_effect=fail_recovery):
   with self.assertRaises(RuntimeError) as cm:self.install_product()
  self.assertIs(cm.exception,recovery);self.assertIs(recovery.__context__,original)
if __name__=='__main__':unittest.main(verbosity=2)
