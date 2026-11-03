"""Removed ASR services: no new requests, original documents/keys stay intact."""
from dataclasses import replace,asdict
from types import SimpleNamespace
import json,time,unittest
from unittest.mock import patch
from src.core import library
from src.core.engines.providers import PRESETS,all_presets
from src.core.engines.registry import available_engines,get_engine
from src.core.engines.cloud_requests import build_cloud_engine
from src.core.models import Transcript
from tests.test_agent_workspace import AgentWorkspaceTests
from tests.test_cli_expansion import ExpansionTests
from tests.test_live_recording import until

class RetiredCloudTests(unittest.TestCase):
    setUp=AgentWorkspaceTests.setUp
    runtime=ExpansionTests.runtime
    invoke=ExpansionTests.invoke

    def old_plan(self,pid,runner=None,count=1):
        service=self.runtime(runner).expansion_batches()
        with patch.dict(PRESETS,{pid:replace(PRESETS[pid],retired=False)}), patch('src.core.agent_workspace.batches.policy_for'):
            plan=service.prepare('retired-'+pid,[dict(source=f'https://example.com/old-{i}.wav') for i in range(count)],pid)
        return service,plan

    def test_new_cli_and_engine_requests_reject_before_secret_read(self):
        with patch('src.core.secrets.get_config',side_effect=AssertionError('removed services must not read keys')):
            for pid in ('openai','groq'):
                code,_=self.invoke('batches.prepare',dict(request_id='removed-'+pid,items=[dict(source='https://example.com/new.wav')],engine=pid))
                self.assertNotEqual(code,0)
                with self.assertRaisesRegex(ValueError,'已移除'):get_engine(pid)
                with self.assertRaisesRegex(ValueError,'已移除'):build_cloud_engine(pid,{'api_key':'fixture'})
        self.assertTrue({'openai','groq'}.isdisjoint(p.id for p in all_presets()))
        self.assertTrue({'openai','groq'}.isdisjoint(pid for pid,name in available_engines()))

    def test_old_pending_batch_can_be_read_but_never_uploaded(self):
        for pid in ('openai','groq'):
            service,plan=self.old_plan(pid)
            before=service._path(plan['batch_id']).read_bytes()
            with patch('src.core.secrets.get_config',side_effect=AssertionError('must not read keys')):
                self.assertEqual(service.show(plan['batch_id'])['engine'],pid)
                with self.assertRaisesRegex(ValueError,'已移除'):
                    service.start(plan['batch_id'],plan['plan_hash'],True,cloud_upload_consent=True,resume=True)
            self.assertFalse(service.workers)
            self.assertEqual(service._path(plan['batch_id']).read_bytes(),before)

    def test_old_saved_result_can_finish_without_network_or_credentials(self):
        for pid in ('openai','groq'):
            calls=[]
            def runner(item,batch,*args):
                calls.append(item['source']);return Transcript('历史完整结果',[],engine=batch['engine'])
            service,plan=self.old_plan(pid,runner)
            with patch.dict(PRESETS,{pid:replace(PRESETS[pid],retired=False)}),patch.object(library,'save',side_effect=OSError('fixture disk')):
                service.start(plan['batch_id'],plan['plan_hash'],True,cloud_upload_consent=True)
                until(lambda:not service.workers)
            self.assertTrue(service._result_path(plan['batch_id'],0).exists())
            service.runner=lambda *args:self.fail('saved result cannot upload')
            with patch('src.core.secrets.get_config',side_effect=AssertionError('must not read keys')):
                service.start(plan['batch_id'],plan['plan_hash'],resume=True)
                until(lambda:not service.workers)
            result=service.show(plan['batch_id'])
            self.assertEqual(result['state'],'succeeded')
            self.assertIn('历史完整结果',library.load(result['items'][0]['document_id']).text)
            self.assertEqual(len(calls),1)

    def cache(self,service,plan,index=0):
        from src.core.agent_workspace.documents import digest
        from src.core.agent_workspace.store import encode
        raw=asdict(Transcript('本机缓存完整正文',[],engine=plan['engine']))
        service._result_path(plan['batch_id'],index).write_text(encode(dict(transcript=raw,sha256=digest(encode(raw)))))

    def test_mixed_cached_and_unsent_history_only_saves_local_result(self):
        service,plan=self.old_plan('openai',count=2)
        self.cache(service,plan)
        before=service.show(plan['batch_id'])['items'][1]
        with patch('src.core.secrets.get_config',side_effect=AssertionError('must not read keys')), patch.object(service,'_prepare_cloud_engine',side_effect=AssertionError('must not build cloud engine')):
            service.start(plan['batch_id'],plan['plan_hash'],resume=True)
            until(lambda:not service.workers)
        result=service.show(plan['batch_id'])
        self.assertEqual(result['counts']['succeeded'],1)
        self.assertEqual(result['items'][1],before)
        self.assertIn('本机缓存完整正文',library.load(result['items'][0]['document_id']).text)

    def test_removed_default_returns_to_local_without_changing_other_choices(self):
        from src import config
        path=self.root/'config.json'
        with patch.object(config,'_CONFIG_PATH',path):
            for pid in ('openai','groq'):
                path.write_text(json.dumps(dict(engine_name=pid)))
                self.assertEqual(config.AppConfig.load().engine_name,config.DEFAULT_ENGINE)
            path.write_text(json.dumps(dict(engine_name='local_whisper_mlx_4bit')))
            self.assertEqual(config.AppConfig.load().engine_name,'local_whisper_mlx_4bit')

    def test_settings_removes_only_asr_entries_and_rejects_stale_forms(self):
        from src.core.model_connections import Service
        from src.core.agent_workspace.store import WorkspaceStore
        store=WorkspaceStore();store.preference('model-settings-cloud-configs',dict(openai={'configured':True},groq={'configured':True}))
        service=Service(SimpleNamespace(engine_name='local_whisper_mlx_4bit'),store=store)
        snapshot=service.snapshot()
        self.assertEqual({p['id'] for p in snapshot['asrProviders']},{'dashscope','volcengine','siliconflow','custom_asr'})
        self.assertTrue({'openai','groq'}.isdisjoint(snapshot['cloudConfigs']))
        with patch('src.core.secrets.get_key',side_effect=AssertionError('removed form must not read keys')):
            for pid in ('openai','groq'):
                with self.assertRaises(ValueError):service.execute('save_connection',dict(purpose='extract',provider=pid,key='fixture',mode='api',region=0))
        self.assertEqual(set(store.preference('model-settings-cloud-configs')),{'openai','groq'})

    def test_gui_selectors_omit_removed_services_and_keep_old_history_readable(self):
        from PySide6.QtWidgets import QApplication,QWidget
        from src.ui.cloud_batch import CloudBatchDialog
        app=QApplication.instance() or QApplication([])
        service,plan=self.old_plan('openai')
        self.cache(service,plan)
        main=QWidget();main.cfg=SimpleNamespace(engine_name='local_whisper_mlx_4bit')
        with patch('src.core.agent_workspace.client.dispatch',side_effect=lambda op,**args:{'batches':service.list()} if op=='batch_list' else service.show(**args) if op=='batch_show' else service.start(**args)), patch('PySide6.QtWidgets.QMessageBox.question',side_effect=AssertionError('local recovery cannot ask upload consent')):
            dialog=CloudBatchDialog(main);dialog.timer.stop()
            end=time.monotonic()+5
            while dialog.worker is not None and time.monotonic()<end:app.processEvents();time.sleep(.005)
            ids={dialog.provider.itemData(i) for i in range(dialog.provider.count())}
            self.assertEqual(ids,{'dashscope','volcengine','siliconflow','custom_openai'})
            self.assertEqual(dialog.selection.currentData(),plan['batch_id'])
            self.assertIn('已移除',dialog.selection.currentText())
            end=time.monotonic()+5
            while (dialog.worker is not None or not getattr(dialog,'batch',None)) and time.monotonic()<end:app.processEvents();time.sleep(.005)
            dialog.resume()
            end=time.monotonic()+5
            while time.monotonic()<end:
                app.processEvents();time.sleep(.005)
                if dialog.worker is None and not service.workers and service.show(plan['batch_id'])['state']=='succeeded':break
            self.assertEqual(service.show(plan['batch_id'])['state'],'succeeded')
            dialog.shutdown()
        dialog.deleteLater();main.deleteLater();app.processEvents()
