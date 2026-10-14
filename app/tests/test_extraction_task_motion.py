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

    def test_many_cards_measurement(self):
        self.widget.timer.stop()
        self.main._add_task('https://example.invalid/1','link','测量样本')
        row=self.widget.rows()[0]
        rows=[dict(row,id=i+1,name=f'隔离任务 {i+1}') for i in range(500)]
        self.js('window.fixture='+json.dumps(self.snapshot(rows),ensure_ascii=False)+'; receive(window.fixture)')
        self.tick()
        result=json.loads(self.js('''JSON.stringify((()=>{
          const times=[], preserved=[];
          for(let i=0;i<8;i++){
            const old=document.querySelector('[data-job="1"]');
            fixture.tasks.push({...fixture.tasks[0],id:501+i,name:'新增 '+i});
            const start=performance.now();receive(fixture);
            document.querySelector('#board').getBoundingClientRect();
            times.push(performance.now()-start);
            preserved.push(old===document.querySelector('[data-job="1"]'));
          }
          return {times,preserved,cards:document.querySelectorAll('.task-card').length};
        })())'''))
        print('TASK_MOTION_MEASUREMENT '+json.dumps(result))
        self.assertEqual(result['cards'],508)
        self.assertTrue(all(result['preserved']))
        output=os.environ.get('LULU_MOTION_METRICS')
        if output:Path(output).write_text(json.dumps(result,indent=2))

    def prepare_rows(self, count=3):
        self.widget.timer.stop()
        for i in range(count):
            self.main._add_task(f'https://example.invalid/{i}','link',f'任务 {i}')
        return self.widget.rows()

    def send(self, rows, prefix=''):
        self.js(prefix+'receive('+json.dumps(self.snapshot(rows),ensure_ascii=False)+')')

    def order(self):
        return json.loads(self.js('JSON.stringify([...document.querySelector(".task-feed").children].map(n=>n.dataset.job||"g:"+n.dataset.group))'))

    def test_newest_first_preserves_execution_and_queue_counts(self):
        rows=self.prepare_rows()
        for row in rows:row['state']='queued'
        self.send(rows)
        self.assertEqual(self.order(),['3','2','1'])
        counts=json.loads(self.js('JSON.stringify([...document.querySelectorAll(".queue-number")].map(n=>n.firstChild.textContent))'))
        self.assertEqual(counts,['2','1','0'])
        self.assertEqual([t.id for t in self.main.tasks],[1,2,3])
        for row in rows:row['state']='waiting'
        self.send(rows)
        self.js('startAll()')
        self.until(lambda:len([v for v in self.main.calls if isinstance(v,tuple) and v[0]=='start'])==3)
        self.assertEqual([v for v in self.main.calls if isinstance(v,tuple)], [('start',1),('start',2),('start',3)])

    def test_batches_are_newest_first_but_members_keep_original_order(self):
        rows=self.prepare_rows(4)
        rows[1].update(group='group-a',groupName='批次 A')
        rows[2].update(group='group-a',groupName='批次 A')
        value=self.snapshot(rows);value['expanded']=['group-a']
        self.js('receive('+json.dumps(value,ensure_ascii=False)+')')
        self.assertEqual(self.order(),['4','g:2','1'])
        self.assertEqual(json.loads(self.js('JSON.stringify([...document.querySelectorAll(".member[data-job]")].map(n=>n.dataset.job))')),['2','3'])

    def test_entrance_once_and_existing_cards_survive_updates(self):
        rows=self.prepare_rows(2)
        self.send(rows[:1], 'receivedSnapshot=false;seenTaskIds.clear();')
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)
        # Send and inspect in one browser turn, before animation can complete.
        self.js('window.oldCard=document.querySelector("[data-job=\\"1\\"]")')
        self.send(rows)
        self.assertTrue(self.js('oldCard===document.querySelector("[data-job=\\"1\\"]")'))
        self.assertTrue(self.js('document.querySelector("[data-job=\\"2\\"]").classList.contains("task-enter")'))
        self.assertFalse(self.js('oldCard.classList.contains("task-enter")'))
        self.until(lambda:self.js('document.querySelectorAll(".task-enter").length')==0)
        rows[0]['state']='running';rows[0]['progress']=.3
        self.send(rows)
        self.assertTrue(self.js('oldCard===document.querySelector("[data-job=\\"1\\"]")'))
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)
        self.send(rows[:1]);self.send(rows)  # undo/reappearing card
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)
        self.js("filter='done';renderBoard();filter='all';renderBoard()")
        self.assertEqual(self.order(),['2','1'])
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)

    def test_top_edge_flip_is_500ms_visible_only_and_does_not_block_buttons(self):
        rows=self.prepare_rows(20)
        self.send(rows[:-1], 'receivedSnapshot=false;seenTaskIds.clear();')
        script='''receive(VALUE);window.newMotion=document.querySelector('[data-job="20"]').getAnimations().find(a=>a.animationName==='task-arrive');if(newMotion){newMotion.pause();newMotion.currentTime=250}'''
        self.js(script.replace('VALUE',json.dumps(self.snapshot(rows),ensure_ascii=False)))
        self.assertTrue(self.js('Boolean(window.newMotion)'))
        self.assertEqual(self.js('newMotion.effect.getTiming().duration'),500)
        self.assertIn('rotateX(-85deg)',self.js('newMotion.effect.getKeyframes()[0].transform'))
        self.assertTrue(self.js('getComputedStyle(newMotion.effect.target).transformOrigin.endsWith(" 0px")'))
        self.assertLessEqual(self.js('document.querySelectorAll(".task-enter").length'),8)
        self.assertFalse(self.js('document.querySelector("[data-job=\\"20\\"] .task-start").disabled'))
        capture=os.environ.get('LULU_EXTRACTION_CAPTURE')
        if capture:
            Path(capture).mkdir(parents=True,exist_ok=True)
            self.widget.grab().save(str(Path(capture)/'task-arrival-midpoint.png'))
        self.js('newMotion.finish()')
        self.until(lambda:self.js('document.querySelectorAll(".task-enter").length')==0)
        self.assertEqual(self.js('getComputedStyle(newMotion.effect.target).transform'),'none')
        # A new card outside the viewport appears without spending motion work.
        self.js('document.querySelector("#main").scrollTop=1500')
        next_row=dict(rows[0],id=21,name='屏幕外新增',displayOrder=rows[-1]['displayOrder']+1)
        self.send([*rows,next_row])
        self.assertFalse(self.js('document.querySelector("[data-job=\\"21\\"]").classList.contains("task-enter")'))

    def test_reduced_motion_disables_new_card_animation(self):
        rows=self.prepare_rows(2)
        self.send(rows[:1])
        self.until(lambda:self.js('document.querySelectorAll(".task-enter").length')==0)
        self.js("Object.defineProperty(motionPreference,'matches',{value:true,configurable:true})")
        self.send(rows)
        self.assertEqual(self.order(),['2','1'])
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)
        self.assertEqual(self.js('document.querySelector("[data-job=\\"2\\"]").getAnimations().length'),0)

    def test_rapid_additions_bound_motion_and_keep_latest_visible(self):
        rows=self.prepare_rows(1)
        self.send(rows, 'receivedSnapshot=false;seenTaskIds.clear();')
        self.js('fixture='+json.dumps(self.snapshot(rows),ensure_ascii=False))
        value=json.loads(self.js('''JSON.stringify((()=>{
          for(let i=2;i<=25;i++){
            fixture.tasks.push({...fixture.tasks[0],id:i,name:'连续加入 '+i});receive(fixture);
          }
          return {active:document.querySelectorAll('.task-enter').length,
            first:document.querySelector('.task-feed').firstElementChild.dataset.job,
            newest:document.querySelector('[data-job="25"]').classList.contains('task-enter')};
        })())'''))
        self.assertLessEqual(value['active'],8)
        self.assertEqual(value['first'],'25')
        self.assertTrue(value['newest'])
        self.until(lambda:self.js('document.querySelectorAll(".task-enter").length')==0)

    def test_real_browser_reduced_motion(self):
        if '--force-prefers-reduced-motion' not in os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS',''):
            self.skipTest('Run separately with browser reduced-motion preference')
        self.assertTrue(self.js("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        rows=self.prepare_rows(2)
        self.send(rows[:1]);self.send(rows)
        self.assertEqual(self.order(),['2','1'])
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)
        self.assertEqual(self.js('document.querySelector("[data-job=\\"2\\"]").getAnimations().length'),0)

    def test_restored_tasks_sort_by_creation_time_not_backend_list_order(self):
        rows=self.prepare_rows(3)
        rows[0]['displayOrder']=30
        rows[1]['displayOrder']=10
        rows[2]['displayOrder']=0  # legacy upgrade history without creation metadata
        self.send(rows,'receivedSnapshot=false;seenTaskIds.clear();')
        self.assertEqual(self.order(),['1','2','3'])
        self.send([rows[2],rows[0],rows[1]],'receivedSnapshot=false;seenTaskIds.clear();')
        self.assertEqual(self.order(),['1','2','3'])
        self.assertEqual(self.js('document.querySelectorAll(".task-enter").length'),0)

    def test_delayed_history_load_does_not_animate(self):
        rows=self.prepare_rows(2)
        for row in rows:row['animateNew']=False
        self.send([])
        self.send(rows)
        self.assertEqual(self.order(),['2','1'])
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

    def test_creation_time_survives_projection_and_adoption(self):
        from src.core.gui_task_ledger import _task_from_job, CoreTaskQueueStore
        from src.core.task import Task
        from datetime import datetime
        task=_task_from_job(self.summary())
        expected=datetime.fromisoformat('2026-09-29T02:00:00+00:00').timestamp()
        self.assertEqual(task._display_created_at,expected)
        pending=Task(id=1,title='待提交',source='fixture',kind='file')
        CoreTaskQueueStore._adopt(pending,task)
        self.assertEqual(pending._display_created_at,expected)

    def test_old_runtime_and_history_metadata_remain_optional(self):
        from src.core.gui_task_ledger import _task_from_job
        value=self.summary()
        for timestamp in (None, 'invalid', '2026-09-29T02:00:00', {}):
            value['gui_task']['_display_created_at']=timestamp
            self.assertEqual(_task_from_job(value)._display_created_at,0)
        del value['gui_task']['_display_created_at']
        self.assertEqual(_task_from_job(value)._display_created_at,0)
