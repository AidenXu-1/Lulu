"""Actual extraction webpage, isolated tasks; no download, models or user data."""
import json
import os
import unittest
from pathlib import Path
from tests.test_extraction_workspace import ExtractionWorkspaceFixture


class ExtractionTaskMotionTests(ExtractionWorkspaceFixture, unittest.TestCase):
    def snapshot(self, rows):
        return dict(tasks=rows, models=self.widget._models, model=self.main.cfg.engine_name,
                    theme='warm', recording=False, source='麦克风', recordFolder=str(self.root),
                    recSeconds=0, expanded=[], skippedGroups=[])


    def prepare_rows(self, count=3):
        self.widget.timer.stop()
        for i in range(count):
            self.main._add_task(f'https://example.invalid/{i}','link',f'任务 {i}')
        return self.widget.rows()

    def send(self, rows, prefix=''):
        self.js(prefix+'receive('+json.dumps(self.snapshot(rows),ensure_ascii=False)+')')

    def order(self):
        return json.loads(self.js('JSON.stringify([...document.querySelector(".task-feed").children].filter(n=>!n.classList.contains("task-exit")).map(n=>n.dataset.job||"g:"+n.dataset.group))'))






    def test_reduced_motion_change_finishes_active_card_reflow(self):
        rows=self.prepare_rows(3)
        self.send(rows[:2], 'receivedSnapshot=false;seenTaskIds.clear();')
        self.send(rows)
        self.assertGreater(self.js('[...boardCards.values()].filter(({node})=>node._reflowAnimation?.playState==="running").length'),0)
        self.js("Object.defineProperty(motionPreference,'matches',{value:true,configurable:true});motionPreference.dispatchEvent(new Event('change'))")
        self.assertEqual(self.js('[...boardCards.values()].filter(({node})=>node._reflowAnimation?.playState==="running").length'),0)
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)







class TaskCreationProjectionTests(unittest.TestCase):
    def summary(self):
        from types import SimpleNamespace
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.job_ledger.contracts import JobState
        frozen={'gui_task':{'title':'任务','kind':'link','source':'https://example.invalid/a'}}
        job=SimpleNamespace(frozen_policy=frozen,job_id='job-example',job_revision=1,
                            state=JobState.PREPARED,job_kind='gui_transcription',input_ref='fixture',
                            partial_failures=[],result_ref=None,created_by='gui',created_at='2026-09-29T02:00:00Z')
        result=LocalTranscriptionCliRuntime._gui_job_summary(job)
        self.assertNotIn('_display_created_at',frozen['gui_task'])
        return result


