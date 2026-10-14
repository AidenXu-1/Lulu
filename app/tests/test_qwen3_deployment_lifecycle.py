"""Persistent dependency lifecycle using isolated real files and receipts."""
import unittest
from unittest.mock import patch
from tests.test_qwen3_modular_deployment import ModularDeploymentTests
from src.core.synthesis import qwen3_deployment as dp, qwen3_weights as qw, runtime_packages as rp

class LifecycleTests(unittest.TestCase):
    install_env=ModularDeploymentTests.install_env
    download=ModularDeploymentTests.download
    def setUp(self):
        ModularDeploymentTests.setUp(self);self.byte_count=0
        p=patch.object(rp,'_snapshot_download_cancellable',side_effect=self.transport);p.start();self.addCleanup(p.stop)
    def transport(self,repo,revision,cache,cancel,*,allow_patterns):
        name=repo.split('/')[-1];self.requests.append((name,tuple(allow_patterns)))
        for rel in allow_patterns:
            if cancel():raise rp.CancelledError()
            dest=cache/('models--'+repo.replace('/','--'))/'snapshots'/revision/rel
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(self.data[name][rel]);self.byte_count+=len(self.data[name][rel])
    def install(self,*names):dp.request(names);dp.run()
    def test_lifecycle_selected_shared_once_and_last_cleanup(self):
        self.install('VoiceDesign')
        self.assertTrue(qw.is_installed(self.root,'VoiceDesign'));self.assertFalse((self.root/'Base').exists())
        before=self.byte_count;self.install('Base','CustomVoice')
        self.assertEqual(self.byte_count-before,sum(len(self.data[n]['model.safetensors']) for n in ('Base','CustomVoice')))
        self.assertEqual(self.env_install.call_count,1)
        user=self.root.parent/'voice-features/saved';user.parent.mkdir();user.write_bytes(b'voice')
        self.requests.clear();self.install(*qw.FEATURES);self.assertEqual(self.requests,[])
        dp.remove('Base');dp.remove('CustomVoice');self.assertTrue(qw.is_installed(self.root,'VoiceDesign'))
        dp.remove('VoiceDesign');self.assertFalse((self.root.parent/'runtime').exists());self.assertFalse((self.root.parent/'shared').exists())
        self.assertEqual(user.read_bytes(),b'voice')
    def test_lifecycle_paused_consumer_protects_and_cancel_last_cleans(self):
        self.install('Base');dp.request(['VoiceDesign'],'paused');dp.remove('Base')
        self.assertTrue((self.root.parent/'runtime').exists());self.assertEqual(dp.removal_plan()['consumers'],['VoiceDesign'])
        with self.assertRaisesRegex(RuntimeError,'使用'):dp.remove()
        dp.request(['VoiceDesign'],'cancelling');dp.run()
        self.assertFalse((self.root.parent/'runtime').exists());self.assertEqual(dp.consumers(),[])
    def test_lifecycle_keep_opt_in_then_cleanup(self):
        self.install('Base');dp.remove('Base',retain_common=True)
        self.assertTrue((self.root.parent/'runtime').exists());self.assertEqual(dp.consumers(),[])
        dp.remove();self.assertFalse((self.root.parent/'runtime').exists())
    def test_lifecycle_pause_shared_resume_reuses_complete_bytes(self):
        def pause(*a,**kw):
            self.transport(*a,**kw);dp.request(['Base'],'paused');raise rp.CancelledError()
        dp.request(['Base'])
        with patch.object(rp,'_snapshot_download_cancellable',side_effect=pause):dp.run()
        self.assertEqual(dp.read_state()['features']['Base']['state'],'paused')
        old=self.byte_count;self.install('Base')
        self.assertEqual(self.byte_count-old,len(self.data['Base']['model.safetensors']))
    def test_lifecycle_cancel_owner_keeps_other_running(self):
        called=False
        def cancel_owner(*a,**kw):
            nonlocal called
            if not called:dp.request(['Base'],'cancelling');called=True
            self.transport(*a,**kw)
        dp.request(['Base','CustomVoice'])
        with patch.object(rp,'_snapshot_download_cancellable',side_effect=cancel_owner):dp.run()
        self.assertFalse((self.root/'Base').exists());self.assertTrue(qw.is_installed(self.root,'CustomVoice'))
        self.assertEqual(sum('speech_tokenizer/common' in paths for _,paths in self.requests),1)
    def test_lifecycle_restart_is_read_only_until_explicit_continue(self):
        dp.request(['Base','CustomVoice']);self.assertEqual(len(dp.read_state()['features']),2);self.assertEqual(self.requests,[])
        dp.request(['Base','CustomVoice'],'paused');self.assertEqual(dp.read_state()['features']['Base']['state'],'paused')
    def test_lifecycle_generation_lease_blocks_remove(self):
        self.install('Base')
        with qw.deployment_lock(self.root):
            with self.assertRaisesRegex(RuntimeError,'生成或管理'):dp.remove('Base')
        self.assertTrue(qw.is_installed(self.root,'Base'))
    def test_lifecycle_failure_keeps_other_and_cleanup_can_retry(self):
        self.install('Base')
        with patch.object(rp,'_snapshot_download_cancellable',side_effect=OSError('offline')):self.install('CustomVoice')
        self.assertEqual(dp.read_state()['features']['CustomVoice']['state'],'failed');self.assertTrue(qw.is_installed(self.root,'Base'))
        with patch.object(rp,'_remove_managed_path',side_effect=OSError('locked')):
            with self.assertRaisesRegex(RuntimeError,'清理未完成'):dp.remove('CustomVoice')
        self.assertEqual(dp.read_state()['features']['CustomVoice']['state'],'cleanup_failed')
        dp.remove('CustomVoice');self.assertTrue(qw.is_installed(self.root,'Base'))
    def test_lifecycle_plan_union_installed_and_inflight(self):
        one=dp.plan(['Base']);all_plan=dp.plan(list(qw.FEATURES))
        self.assertEqual(all_plan['download']-one['download'],sum(len(self.data[n]['model.safetensors']) for n in ('CustomVoice','VoiceDesign')))
        dp.request(['Base']);self.assertEqual(dp.plan(['CustomVoice'])['download'],len(self.data['CustomVoice']['model.safetensors']))
        self.install(*qw.FEATURES);self.assertEqual(dp.plan(list(qw.FEATURES))['download'],0)

    def test_cross_process_requests_share_one_writer_and_environment(self):
        import json,subprocess,sys
        from pathlib import Path
        (self.home/'locks.json').write_text(json.dumps(self.locks))
        (self.home/'data.json').write_text(json.dumps({n:{p:b.hex() for p,b in files.items()} for n,files in self.data.items()}))
        script=Path(__file__).with_name('qwen_deployment_child.py')
        children=[subprocess.Popen([sys.executable,'-B',str(script),str(self.home),name],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for name in ('Base','CustomVoice')]
        try:
            for child in children:
                out,err=child.communicate(timeout=15);self.assertEqual(child.returncode,0,err.decode())
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait()
        rows=[json.loads(s) for s in (self.home/'requests.jsonl').read_text().splitlines()]
        self.assertEqual(rows.count(['runtime']),1)
        self.assertEqual(sum(len(row)>1 and 'speech_tokenizer/common' in row[1] for row in rows),1)

    def test_restarted_session_does_not_resume_other_stale_queue(self):
        dp.request(['Base','CustomVoice'])
        with patch.object(dp,'SESSION','after-restart'):
            self.install('Base')
        self.assertTrue(qw.is_installed(self.root,'Base'));self.assertFalse(qw.is_installed(self.root,'CustomVoice'))
        self.assertEqual(dp.read_state()['features']['CustomVoice']['state'],'queued')

    def test_recovery_precedes_shared_download(self):
        import json,shutil
        self.install('Base');self.requests.clear()
        tx=self.root/'.qwen-install-crash';tx.mkdir()
        receipt=qw._receipt_path(self.home/'private/.artifact-trust','qwen3-weights:Base')
        shutil.copyfile(receipt,tx/'previous-receipt.json');(tx/'pending.json').write_text(json.dumps({'name':'Base'}))
        (self.root/'Base').rename(tx/'previous');shutil.rmtree(self.root.parent/'shared')
        self.install('Base');self.assertEqual(self.requests,[]);self.assertTrue(qw.is_installed(self.root,'Base'))
