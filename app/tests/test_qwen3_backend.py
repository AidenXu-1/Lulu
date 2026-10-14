"""Persistence and subprocess cancellation without running a neural model."""
import copy,json,sys,tempfile,time,unittest,wave
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis import qwen3_runtime as runtime
from src.core.synthesis.qwen3_worker import prompt_key
from src.core.synthesis.qwen3_subprocess import QwenProcess,Qwen3Engine
from src.core.synthesis.base import VoiceProfile,SynthesisRequest,SynthesisError
from src.core.synthesis.voxcpm2 import _Cancelled

class BackendTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_reference_audio_text_mode_and_revision_invalidate_features(self):
        ref=self.root/'ref.wav';ref.write_bytes(b'original')
        payload={'generate':{'ref_audio':str(ref),'ref_text':'准确原文','x_vector_only_mode':False},'revision':'version1'}
        old=prompt_key(payload)
        for key,value in [('ref_text','修改后的原文'),('x_vector_only_mode',True)]:
            other=copy.deepcopy(payload);other['generate'][key]=value;self.assertNotEqual(prompt_key(other),old)
        other=copy.deepcopy(payload);other['revision']='version2';self.assertNotEqual(prompt_key(other),old)
        ref.write_bytes(b'changed');self.assertNotEqual(prompt_key(payload),old)
    def test_wrong_model_type_and_unconfirmed_revision_fail_closed(self):
        model=self.root/'models/Base';model.mkdir(parents=True)
        for name in ('config.json','generation_config.json','model.safetensors','tokenizer_config.json','vocab.json','merges.txt','speech_tokenizer/config.json','speech_tokenizer/model.safetensors'):
            path=model/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}')
        with patch.dict('os.environ',{'LULU_QWEN3_HOME':str(self.root)}),patch.object(runtime,'root',return_value=self.root),patch.object(runtime,'python_path',return_value=Path(sys.executable)):
            self.assertFalse(runtime.readiness('clone')[0])
            (model/'.lulu-qwen-model.json').write_text(json.dumps({'model':'Base','revision':runtime.REVISIONS['clone']}))
            self.assertFalse(runtime.readiness('clone')[0])
            (model/'config.json').write_text(json.dumps({'tts_model_size':'1b7','tts_model_type':'base'}))
            self.assertTrue(runtime.readiness('clone')[0])
    def test_cancel_terminates_child_and_releases_busy_state(self):
        import subprocess
        worker=self.root/'slow.py';worker.write_text("import sys,time\nsys.stdin.readline()\nprint('{\"type\":\"progress\",\"message\":\"generating\"}',flush=True)\ntime.sleep(60)\n")
        original=subprocess.Popen;children=[];cancel={'value':False};manager=QwenProcess()
        def start(args,**kw):
            args=list(args);args[-1]=str(worker);proc=original(args,**kw);children.append(proc);return proc
        def progress(*_):cancel['value']=True
        try:
            with patch.object(runtime,'readiness',return_value=(True,'')),patch.object(runtime,'root',return_value=self.root),patch.object(runtime,'python_path',return_value=Path(sys.executable)),patch('src.core.synthesis.qwen3_subprocess.subprocess.Popen',side_effect=start):
                with self.assertRaises(_Cancelled):manager.run({'model_path':'test','revision':'1','mode':'clone'},progress,lambda:cancel['value'])
            self.assertIsNotNone(children[0].poll());self.assertFalse(manager.status()['alive']);self.assertFalse(manager.status()['busy'])
        finally:manager.release()
    def test_existing_output_is_never_replaced(self):
        ref=self.root/'ref.wav';ref.write_bytes(b'reference');dest=self.root/'saved.wav';dest.write_bytes(b'KEEP')
        request=SynthesisRequest('你好',VoiceProfile('test','声音',str(ref),'参考文字'),dest,engine_options={})
        def fake(payload,*_):
            for key in ('output_path','raw_path'):
                with wave.open(payload[key],'wb') as f:f.setparams((1,2,24000,0,'NONE',''));f.writeframes(b'\0\1'*2400)
            return {'duration':.1,'sample_rate':24000,'seconds':1,'cache_hit':False}
        with patch.object(runtime,'readiness',return_value=(True,'')),patch.object(runtime,'revision',return_value='fixed'),patch('src.core.synthesis.qwen3_subprocess.PROCESS.run',side_effect=fake),patch('src.core.synthesis.indextts2_subprocess.shutdown_persistent_worker'):
            with self.assertRaisesRegex(SynthesisError,'已存在'):Qwen3Engine().synthesize(request)
        self.assertEqual(dest.read_bytes(),b'KEEP');self.assertFalse((self.root/'saved.raw.wav').exists())

    def test_direct_index_worker_cannot_overlap_active_qwen(self):
        from types import SimpleNamespace
        from src.core.synthesis.indextts2_subprocess import IndexTTS2SubprocessEngine
        ref=self.root/'ref.wav';ref.write_bytes(b'reference')
        req=SynthesisRequest('你好',VoiceProfile('test','声音',str(ref),'原文'),self.root/'index.wav')
        with patch('src.core.synthesis.indextts2_subprocess.check_runtime',return_value=SimpleNamespace(ready=True)),patch('src.core.synthesis.qwen3_subprocess.shutdown_persistent_worker',return_value=False):
            with self.assertRaisesRegex(SynthesisError,'Qwen 正在生成'):IndexTTS2SubprocessEngine().synthesize(req)
