"""Resolved link titles without changing frozen inputs, local names or saved edits."""
import unittest
from unittest.mock import patch
from src.core import library
from src.core.agent_workspace.batches import BatchService
from src.core.models import Transcript
from tests.test_extraction_batches import ExtractionBatchTests
from tests.test_live_recording import until


class ExtractionTitleTests(ExtractionBatchTests):
    def run_title(self, request, *, title=None, meta=None, source=None, label='链接转写'):
        service=BatchService(None,runner=lambda item,batch,*a:Transcript('视频正文',[],
            engine=batch['engine'],title=title,meta=meta or {}))
        plan=service.stage_extraction(request,[dict(source=source or 'https://example.com/'+request,title=label,outputs=['text'])])
        self.control(service,plan,'start',network_consent=True)
        until(lambda:not service.workers)
        row=service.show(plan['batch_id'])['items'][0]
        return service,plan,row,library.document_snapshot(row['document_id'])

    def test_resolved_video_title_is_saved_and_survives_batch_reload(self):
        actual='公安让去退赃，先搞清楚这3个问题'
        service,plan,row,doc=self.run_title('real-title',title=actual)
        self.assertEqual(doc['title'],actual)
        self.assertEqual(row['title'],actual)
        self.assertIn(actual,doc['text'])
        self.assertEqual(service._read(plan['batch_id'])['items'][0]['title'],'链接转写')
        reopened=BatchService(None,runner=lambda *a:self.fail('重新读取不能再识别'))
        self.assertEqual(reopened.show(plan['batch_id'])['items'][0]['title'],actual)
        self.assertEqual(reopened._read(plan['batch_id'])['plan_hash'],plan['plan_hash'])

    def test_checkpoint_metadata_title_is_used_when_transcript_title_is_empty(self):
        _,_,row,doc=self.run_title('metadata-title',title=' ',meta={'title':'真实视频标题'})
        self.assertEqual(doc['title'],'真实视频标题')
        self.assertEqual(row['title'],'真实视频标题')

    def test_missing_or_invalid_remote_title_keeps_original_name(self):
        for number,title in enumerate((None,' ',123)):
            with self.subTest(title=title):
                _,_,row,doc=self.run_title('fallback-'+str(number),title=title)
                self.assertEqual(doc['title'],'链接转写')
                self.assertEqual(row['title'],'链接转写')

    def test_local_filename_is_not_replaced_by_recognizer_metadata(self):
        source=self.root/'访谈录音.wav';source.write_bytes(b'isolated local input')
        _,_,row,doc=self.run_title('local-title',title='内部音频名',meta={'title':'内部信息'},source=str(source),label=source.name)
        self.assertEqual(row['title'],source.name)
        self.assertEqual(doc['title'],source.name)

    def test_save_retry_uses_cached_real_title_without_another_recognition(self):
        calls=[]
        def run(item,batch,*a):
            calls.append(1);return Transcript('已有正文',[],engine=batch['engine'],title='缓存中的视频标题')
        service=BatchService(None,runner=run)
        plan=service.stage_extraction('cached-title',[dict(source='https://example.com/cached',title='链接转写',outputs=['text'])])
        with patch.object(library,'save',side_effect=OSError('模拟保存失败')):
            self.control(service,plan,'start',network_consent=True);until(lambda:not service.workers)
        self.control(service,plan,'check',[0]);until(lambda:not service.workers)
        row=service.show(plan['batch_id'])['items'][0]
        self.assertEqual(library.document_snapshot(row['document_id'])['title'],'缓存中的视频标题')
        self.assertEqual(calls,[1])


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(ExtractionTitleTests(name) for name in ExtractionTitleTests.__dict__ if name.startswith('test_'))
