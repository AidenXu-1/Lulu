"""Isolated feature transactions: actual files, receipts, locks and selective transport."""
import hashlib
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from src.core.synthesis import qwen3_weights as qw, runtime_packages as rp


class ModularDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.root = self.home/'qwen'/'models'
        self.data = {n: {'model.safetensors': n.encode()*12, 'speech_tokenizer/common': b'common pinned data'} for n in qw.FEATURES}
        self.locks = {n: {'repo': 'Qwen/'+n, 'revision': str(i)*40, 'files': [
            {'path': p, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for p, b in files.items()
        ]} for i, (n, files) in enumerate(self.data.items(), 1)}
        self.requests = []; self.runtime = False
        for p in (patch.object(qw, 'MODELS', self.locks), patch.object(qw.mm, 'app_data_dir', return_value=self.home/'private'),
                  patch.object(rp, 'model_dir', return_value=self.root.parent),
                  patch.object(rp, 'is_runtime_installed', side_effect=lambda _: self.runtime),
                  patch.object(rp, '_snapshot_download_cancellable', side_effect=self.download)):
            p.start(); self.addCleanup(p.stop)
        self.env_install = patch.object(rp, 'install_runtime', side_effect=self.install_env).start()
        self.addCleanup(patch.stopall)

    def install_env(self, *a, **kw):
        self.runtime = True; (self.root.parent/'runtime').mkdir(parents=True, exist_ok=True)
        (self.root.parent/'runtime/python').write_bytes(b'environment')

    def download(self, repo, revision, cache, cancel, *, allow_patterns):
        name = repo.split('/')[-1]; self.requests.append((name, tuple(allow_patterns)))
        for rel in allow_patterns:
            dest = qw._snapshot(self.root, name)/rel; dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.data[name][rel])

    def test_each_feature_alone_and_one_environment_across_three(self):
        for index, name in enumerate(self.data):
            qw.install_feature(name)
            self.assertTrue(qw.is_installed(self.root, name))
            self.assertEqual([n for n in self.data if qw.is_installed(self.root, n)], list(self.data)[:index+1])
        self.assertEqual(self.env_install.call_count, 1)
        self.assertEqual([len(paths) for _, paths in self.requests], [2, 1, 1])
        a = self.root/'Base/speech_tokenizer/common'; b = self.root/'CustomVoice/speech_tokenizer/common'
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)

    def test_repeat_valid_installs_do_not_call_transport_or_environment(self):
        for name in self.data: qw.install_feature(name)
        self.requests.clear(); self.env_install.reset_mock()
        for name in self.data: qw.install_feature(name)
        self.assertEqual(self.requests, []); self.env_install.assert_not_called()

    def test_uninstall_one_preserves_other_receipts_and_user_results(self):
        for name in self.data: qw.install_feature(name)
        user = self.root.parent/'voices'; user.mkdir(); (user/'saved.wav').write_bytes(b'user')
        qw.uninstall_feature('CustomVoice', root=self.root)
        self.assertFalse(qw.is_installed(self.root, 'CustomVoice'))
        for n in ('Base', 'VoiceDesign'): self.assertTrue(qw.is_installed(self.root, n))
        self.assertEqual((user/'saved.wav').read_bytes(), b'user')
        self.assertTrue((self.root.parent/'runtime/python').exists())
        qw.uninstall_feature('Base', root=self.root); qw.uninstall_feature('VoiceDesign', root=self.root, retain_common=True)
        self.assertTrue(any((self.root.parent/'shared').iterdir()))
        self.requests.clear(); qw.install_feature('Base')
        self.assertEqual(self.requests, [('Base', ('model.safetensors',))])

    def test_repair_downloads_only_damaged_file(self):
        qw.install_feature('Base'); self.requests.clear()
        (self.root/'Base/model.safetensors').write_bytes(b'bad')
        qw.install_feature('Base')
        self.assertEqual(self.requests, [('Base', ('model.safetensors',))])
        self.assertTrue(qw.is_installed(self.root, 'Base'))

    def test_cancelled_download_keeps_complete_files_for_next_feature(self):
        def partial(repo, rev, cache, cancel, *, allow_patterns):
            self.download(repo, rev, cache, cancel, allow_patterns=['speech_tokenizer/common'])
            raise rp.CancelledError()
        with patch.object(rp, '_snapshot_download_cancellable', side_effect=partial):
            with self.assertRaises(rp.CancelledError): qw.install_feature('Base')
        self.requests.clear(); qw.install_feature('CustomVoice')
        self.assertEqual(self.requests, [('CustomVoice', ('model.safetensors',))])
        self.assertFalse(qw.is_installed(self.root, 'Base'))

    def test_concurrent_requests_serialize_and_only_download_shared_once(self):
        entered = threading.Event(); proceed = threading.Event(); errors = []
        def slow(*a, **kw):
            if a[0].endswith('Base'): entered.set(); self.assertTrue(proceed.wait(5))
            self.download(*a, **kw)
        def run(name):
            try: qw.install_feature(name)
            except BaseException as exc: errors.append(exc)
        with patch.object(rp, '_snapshot_download_cancellable', side_effect=slow):
            a = threading.Thread(target=run, args=('Base',)); b = threading.Thread(target=run, args=('CustomVoice',))
            a.start(); self.assertTrue(entered.wait(5)); b.start(); proceed.set(); a.join(10); b.join(10)
        self.assertFalse(a.is_alive()); self.assertFalse(b.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(sum('speech_tokenizer/common' in paths for _, paths in self.requests), 1)
        self.assertEqual(self.env_install.call_count, 1)

    def test_waiting_install_can_cancel_without_affecting_owner(self):
        with qw.deployment_lock(self.root):
            with self.assertRaises(rp.CancelledError):
                qw.install_feature('Base', is_cancelled=lambda: True)
        self.assertEqual(self.requests, [])

    def test_existing_legacy_feature_reuses_shared_without_modifying_it(self):
        qw.install_feature('Base')
        # A valid old full-copy layout, no new content pool.
        old = self.root/'Base/speech_tokenizer/common'; data = old.read_bytes(); old.unlink(); old.write_bytes(data)
        qw.write_install_receipt('qwen3-weights:Base', self.root/'Base', self.locks['Base'], self.home/'private/.artifact-trust')
        shutil.rmtree(self.root.parent/'shared'); self.requests.clear()
        qw.install_feature('VoiceDesign')
        self.assertEqual(self.requests, [('VoiceDesign', ('model.safetensors',))])
        self.assertTrue(qw.is_installed(self.root, 'Base'))

    def test_low_space_preserves_original_and_starts_no_network(self):
        with patch.object(shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaisesRegex(RuntimeError, '空间不足'): qw.install_feature('Base')
        self.assertEqual(self.requests, []); self.assertFalse(qw.is_installed(self.root, 'Base'))

    def test_failed_receipt_rolls_back_selected_model(self):
        qw.install_feature('Base'); (self.root/'Base/model.safetensors').write_bytes(b'prior damaged')
        with patch.object(qw, 'write_install_receipt', side_effect=RuntimeError('receipt failed')):
            with self.assertRaisesRegex(RuntimeError, 'receipt failed'): qw.install_feature('Base')
        self.assertEqual((self.root/'Base/model.safetensors').read_bytes(), b'prior damaged')

    def test_restart_recovers_directory_swap_without_network(self):
        import json
        qw.install_feature('Base'); self.requests.clear()
        transaction = self.root/'.qwen-install-interrupted'; transaction.mkdir()
        receipt = qw._receipt_path(self.home/'private/.artifact-trust', 'qwen3-weights:Base')
        shutil.copyfile(receipt, transaction/'previous-receipt.json')
        (transaction/'pending.json').write_text(json.dumps({'name': 'Base'}))
        (self.root/'Base').replace(transaction/'previous')
        qw.install_feature('Base')
        self.assertTrue(qw.is_installed(self.root, 'Base')); self.assertEqual(self.requests, [])


from tests.model_settings_fixtures import ControllerCase, FakeWorker, m
from src.core.synthesis import qwen3_deployment as dp

class DeploymentControllerTests(ControllerCase):
    def setUp(self):
        super().setUp()
        self.phases={};self.created=[]
        owner=self
        class Worker(FakeWorker):
            def __init__(self,name=None,*,operation,**kwargs):
                super().__init__(name,operation=operation,**kwargs)
                self.name,self.operation=name,operation
                self.result={'names':name,'download':100,'disk':100} if operation=='plan' else {'consumers':['CustomVoice'],'own':10,'common':20,'total':30}
                owner.created.append(self)
        def request(names,action='queued'):
            for name in names:self.phases[name]={'state':action}
        for p in (patch.object(m,'QwenFeatureWorker',Worker),patch.object(dp,'request',side_effect=request),patch.object(dp,'read_state',side_effect=lambda:{'features':self.phases}),patch.object(m.dialogs,'confirm',return_value=True)):
            p.start();self.addCleanup(p.stop)

    def install(self,name):
        self.c.feature_action(name,'install');self.created[-1].finish()
        return self.created[-1]

    def test_selected_features_share_one_install_owner(self):
        owner=self.install('preset');self.c.feature_action('design','install')
        self.assertEqual(sum(w.operation=='install' for w in self.created),1)
        self.assertEqual(set(self.phases),{'CustomVoice','VoiceDesign'})
        self.assertIn(owner,self.c.owned_workers())

    def test_pause_and_cancel_target_only_requested_consumer(self):
        owner=self.install('clone');self.c.feature_action('preset','install')
        self.c.feature_action('clone','pause')
        self.assertEqual(self.phases['Base']['state'],'paused')
        self.assertEqual(self.phases['CustomVoice']['state'],'queued')
        self.c.feature_action('clone','cancel')
        self.assertEqual(self.phases['Base']['state'],'cancelling')
        self.assertEqual(self.phases['CustomVoice']['state'],'queued')
        self.assertFalse(owner.cancel_requested)

    def test_remove_preserves_shared_components_and_other_consumers(self):
        self.c.feature_action('clone','remove');self.created[-1].finish()
        owner=self.created[-1]
        self.assertEqual(owner.name,'Base');self.assertEqual(owner.operation,'uninstall')
        self.assertTrue(owner.kwargs['retain_common'])

    def test_quoted_plan_rechecks_usage_after_confirmation(self):
        self.c.feature_action('clone','install')
        self.c.model_in_use=lambda mid:'正在使用'
        self.created[-1].finish()
        self.assertEqual(sum(w.operation=='install' for w in self.created),0)
        self.assertTrue(any('正在使用' in n for n in self.notices))

    def test_shutdown_never_starts_pending_feature(self):
        owner=self.install('clone');self.c.feature_action('design','install')
        self.assertFalse(self.c.shutdown_workers(0));self.assertTrue(owner.cancel_requested)
        owner.finish()
        self.assertEqual(sum(w.operation=='install' for w in self.created),1)
        self.assertFalse(self.c.has_active_work())

    def test_owner_completion_clears_transient_jobs_for_all_features(self):
        owner=self.install('clone');self.c.feature_action('design','install')
        owner.finish()
        self.assertFalse(any(k.startswith('feature:') for k in self.c.jobs))

    def test_restart_exposes_paused_resume_without_starting_install(self):
        with self.public_state(read_state={'features':{'Base':{'state':'paused'},'VoiceDesign':{'state':'queued'}}}):
            value=self.c._read_state()
        self.assertTrue(value['featureJobs']['feature:clone']['paused'])
        self.assertTrue(value['featureJobs']['feature:design']['paused'])
        self.assertEqual(self.created,[])
