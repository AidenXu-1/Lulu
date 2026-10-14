import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtGui import QCloseEvent

from src.core.gui_task_recovery import GuiTaskRecoveryStore
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.task import Task, TaskGroup, TaskGroupMember, TaskState
from src.core.task_queue_store import TaskQueueSnapshot
from src.core.models import Transcript
from tests import test_frontend_functional_repair as frontend_fixture


class GuiTaskRecoveryTests(unittest.TestCase):
    def test_core_hidden_cancelled_job_is_not_recreated_or_retried_by_old_draft(self):
        from tests.test_shared_core_gui_task_ledger import _runtime
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.job_ledger.service import JobControlService
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            ledger = JobLedgerStore(root/'ledger', FakeIntegrityAnchorStore())
            runtime = _runtime(JobControlService(ledger), ['gui-job-old', 'old-a', 'old-lineage'])
            row = runtime.gui_jobs_create({'title':'取消输入', 'source':'old.wav', 'kind':'file',
                'folder_id':None, 'stable_key':'old', 'execution_policy':None})
            row = runtime.gui_jobs_transition(job_id=row['job_id'], expected_revision=1,
                state='cancelled', result_ref=None)
            from src.core.gui_task_ledger import _task_from_job
            recovery = GuiTaskRecoveryStore(root/'recovery.json')
            recovery.save([_task_from_job(row)], [])
            runtime.gui_jobs_view(groups=[], hidden_ids=[row['job_id']], expected_revision=0)
            before = ledger.get_job(row['job_id']); journal = ledger.journal_path.read_bytes()
            original = recovery.path.read_bytes()
            def dispatch(command, arguments):
                if command == 'gui.jobs.list': return runtime.gui_jobs_list(**arguments)
                self.fail('Recovery must not recreate or transition this hidden job: '+command)
            queue = CoreTaskQueueStore(dispatch, async_write=True)
            snapshot, found = recovery.merge(queue.load(), authoritative_hidden_ids=queue.hidden_ids)
            self.assertTrue(found)
            self.assertEqual(snapshot.tasks, ())
            self.assertTrue(queue.save(snapshot.tasks, snapshot.groups))
            self.assertEqual(ledger.get_job(row['job_id']), before)
            self.assertEqual(ledger.journal_path.read_bytes(), journal)
            self.assertEqual(recovery.path.read_bytes(), original)

    def test_missing_cancelled_draft_keeps_cancellation_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            recovery = GuiTaskRecoveryStore(Path(tmp)/'recovery.json')
            task = Task('gui-job-cancelled', '取消输入', 'old.wav', 'file', state=TaskState.CANCELLED)
            recovery.save([task], [])
            original = recovery.path.read_bytes()
            snapshot, _ = recovery.merge(TaskQueueSnapshot((), ()))
            self.assertEqual(snapshot.tasks[0].state, TaskState.CANCELLED)
            self.assertTrue(snapshot.tasks[0]._core_pending_create)
            self.assertEqual(recovery.path.read_bytes(), original)

    def test_hidden_intents_survive_unread_save_and_keep_history_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = Task('gui-job-old', '移除输入', 'old.wav', 'file')
            new = Task('gui-job-new', '保留输入', 'new.wav', 'file')
            old_group = TaskGroup(1, '移除组', [TaskGroupMember('', old.title, task_id=old.id)])
            new_group = TaskGroup(2, '保留组', [TaskGroupMember('', new.title, task_id=new.id)])
            store.save([old], [old_group], hidden_ids={old.id})
            store.save([new], [new_group], preserve_existing=True)
            result, found = store.merge(TaskQueueSnapshot((old, new), (old_group, new_group)))
            self.assertTrue(found)
            self.assertEqual([t.id for t in result.tasks], [new.id])
            self.assertEqual([g.name for g in result.groups], ['保留组'])
            self.assertEqual(store.hidden_ids, {old.id})
            self.assertEqual(old.source, 'old.wav')
            self.assertEqual(old_group.members[0].task_id, old.id)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_hidden_intents_keep_original_recovery_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            store.save([], [])
            raw = json.loads(store.path.read_text())
            raw['hidden_ids'] = ['../other-path']
            store.path.write_text(json.dumps(raw))
            original = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.merge(TaskQueueSnapshot((), ()))
            with self.assertRaises(ValueError):
                store.save([], [], preserve_existing=True)
            self.assertEqual(store.path.read_bytes(), original)

    def test_distinct_long_skipped_batches_with_same_prefix_keep_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            first = TaskGroup(1, '同前缀' * 200 + '甲', [TaskGroupMember('same', '同说明' * 1000 + '甲', skipped_reason='无音频')])
            second = TaskGroup(2, '同前缀' * 200 + '乙', [TaskGroupMember('same', '同说明' * 1000 + '乙', skipped_reason='无音频')])
            store.save([], [second])
            result, _ = store.merge(TaskQueueSnapshot((), (first,)))
            self.assertEqual(len(result.groups), 2)
            self.assertEqual({g.id for g in result.groups}, {1, 2})
            self.assertEqual({g.name for g in result.groups}, {first.name, second.name})

    def test_unread_long_skipped_batches_keep_full_text_when_ids_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = TaskGroup(1, '同前缀' * 200 + '甲', [TaskGroupMember('same', '同说明' * 1000 + '甲', skipped_reason='无音频')])
            new = TaskGroup(1, '同前缀' * 200 + '乙', [TaskGroupMember('same', '同说明' * 1000 + '乙', skipped_reason='无音频')])
            store.save([], [old])
            store.save([], [new], preserve_existing=True)
            result, _ = store.merge(TaskQueueSnapshot((), ()))
            self.assertEqual(len(result.groups), 2)
            self.assertEqual({g.name for g in result.groups}, {old.name, new.name})
            self.assertEqual({m.title for g in result.groups for m in g.members},
                             {old.members[0].title, new.members[0].title})
            self.assertEqual(len({g.id for g in result.groups}), 2)

    def test_restored_colliding_batches_persist_in_the_real_core_view(self):
        from src.core.gui_task_view import GuiTaskViewStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = GuiTaskRecoveryStore(root / 'recovery.json')
            old = Task('gui-job-old', '旧输入', 'old.wav', 'file')
            new = Task('gui-job-new', '新输入', 'new.wav', 'file')
            old_group = TaskGroup(1, '旧批次', [TaskGroupMember('', old.title, old.source, old.kind, old.id)], expanded=True)
            new_group = TaskGroup(1, '新批次', [TaskGroupMember('', new.title, new.source, new.kind, new.id)])
            recovery.save([new], [new_group])
            snapshot, _ = recovery.merge(TaskQueueSnapshot((old,), (old_group,)))
            for task in snapshot.tasks:
                task._core_pending_create = False
                task._core_state = 'prepared'
                task._core_job_revision = 1
            view = GuiTaskViewStore(root / 'core')
            known_ids = {t.id for t in snapshot.tasks}
            def dispatch(command, arguments):
                self.assertEqual(command, 'gui.jobs.view')
                return view.save_view(**arguments, known_ids=known_ids)
            CoreTaskQueueStore(dispatch, async_write=True).save(snapshot.tasks, snapshot.groups)
            saved = view.load()
            self.assertEqual({g['name'] for g in saved['groups']}, {'旧批次', '新批次'})
            self.assertEqual(len({g['id'] for g in saved['groups']}), 2)
            self.assertEqual({m['task_id'] for g in saved['groups'] for m in g['members']}, known_ids)
            self.assertTrue(next(g for g in saved['groups'] if g['name'] == '旧批次')['expanded'])

    def test_separate_attempts_of_same_work_keep_separate_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = Task('gui-job-old', '同一作品', 'old.wav', 'file')
            new = Task('gui-job-new', '同一作品', 'old.wav', 'file')
            old_group = TaskGroup(1, '旧批次', [TaskGroupMember('same-work', old.title, old.source, old.kind, old.id)])
            new_group = TaskGroup(1, '新批次', [TaskGroupMember('same-work', new.title, new.source, new.kind, new.id)])
            store.save([old], [old_group])
            store.save([new], iter([new_group]))
            result, _ = store.merge(TaskQueueSnapshot((), ()))
            self.assertEqual({g.name for g in result.groups}, {'旧批次', '新批次'})
            self.assertEqual(len({g.id for g in result.groups}), 2)

    def test_same_batch_keeps_current_edits_after_renumbering_and_retry(self):
        from src.core.task import merge_task_groups
        current = TaskGroup(2, '已修改名称', [TaskGroupMember('', '重试', task_id='gui-job-retry',
                            attempt_task_ids=['gui-job-old', 'gui-job-retry'])], expanded=True)
        restored = TaskGroup(1, '旧名称', [TaskGroupMember('', '上次输入', task_id='gui-job-old')])
        result = merge_task_groups([current], [restored])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], current)
        self.assertTrue(result[0].expanded)
        self.assertEqual(result[0].name, '已修改名称')

    def test_colliding_skipped_only_groups_keep_names_reasons_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = TaskGroup(1, '旧跳过批次', [TaskGroupMember('old', '旧作品', skipped_reason='上次不可用')], expanded=True)
            new = TaskGroup(1, '新跳过批次', [TaskGroupMember('new', '新作品', skipped_reason='本次不可用')])
            store.save([], [old])
            store.save([], [new])
            result, _ = store.merge(TaskQueueSnapshot((), ()))
            self.assertEqual(len(result.groups), 2)
            self.assertEqual(len({g.id for g in result.groups}), 2)
            self.assertEqual({m.skipped_reason for g in result.groups for m in g.members}, {'上次不可用', '本次不可用'})

    def test_unread_draft_preserves_colliding_groups_and_survives_repeated_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = Task('gui-job-old', '旧输入', 'old.wav', 'file')
            new = Task('gui-job-new', '新输入', 'new.wav', 'file')
            old_group = TaskGroup(1, '旧批次', [TaskGroupMember('', old.title, old.source, old.kind, old.id)], expanded=True)
            new_group = TaskGroup(1, '新批次', [TaskGroupMember('', new.title, new.source, new.kind, new.id)])
            store.save([old], [old_group])
            for _ in range(2):
                store.save([new], [new_group], preserve_existing=True)
                result, found = store.merge(TaskQueueSnapshot((), ()))
                self.assertTrue(found)
                groups = {g.name: g for g in result.groups}
                self.assertEqual(set(groups), {'旧批次', '新批次'})
                self.assertNotEqual(groups['旧批次'].id, groups['新批次'].id)
                self.assertEqual(groups['旧批次'].members[0].task_id, old.id)
                self.assertEqual(groups['新批次'].members[0].task_id, new.id)
                self.assertTrue(groups['旧批次'].expanded)
                self.assertEqual({t.id for t in result.tasks}, {old.id, new.id})

    def test_core_history_and_colliding_private_group_both_restore_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp) / 'recovery.json')
            old = Task('gui-job-old', '旧输入', 'old.wav', 'file')
            new = Task('gui-job-new', '新输入', 'new.wav', 'file')
            old_group = TaskGroup(1, '旧批次', [TaskGroupMember('', old.title, old.source, old.kind, old.id)])
            new_group = TaskGroup(1, '新批次', [TaskGroupMember('', new.title, new.source, new.kind, new.id)])
            store.save([old], [old_group])
            store.save([new], [new_group], preserve_existing=True)
            result, found = store.merge(TaskQueueSnapshot((old,), (old_group,)))
            self.assertTrue(found)
            self.assertEqual({g.name for g in result.groups}, {'旧批次', '新批次'})
            self.assertEqual(len(result.groups), 2)
            self.assertEqual(len({g.id for g in result.groups}), 2)
            self.assertEqual({m.task_id for g in result.groups for m in g.members}, {old.id, new.id})

    def test_recovery_never_replays_start_or_overwrites_core_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GuiTaskRecoveryStore(Path(tmp)/'recovery.json')
            queued = Task('gui-job-a','排队输入','a.wav','file',state=TaskState.QUEUED)
            running = Task('gui-job-b','运行输入','b.wav','file',state=TaskState.RUNNING)
            running.transcript = Transcript(text='尚未保存的重要文稿')
            group=TaskGroup(1,'恢复组',[TaskGroupMember('',t.title,t.source,t.kind,t.id) for t in (queued,running)])
            store.save([queued,running],[group])
            self.assertEqual(store.path.stat().st_mode & 0o777,0o600)
            completed = Task(queued.id,queued.title,queued.source,'file',state=TaskState.DONE,library_id='saved')
            result, found=store.merge(TaskQueueSnapshot((completed,),()))
            self.assertTrue(found)
            self.assertIs(result.tasks[0],completed)
            self.assertEqual(result.tasks[0].state,TaskState.DONE)
            self.assertEqual(result.tasks[1].state,TaskState.FAILED)
            self.assertEqual(CoreTaskQueueStore._desired_state(result.tasks[1]), 'prepared')
            self.assertEqual(result.tasks[1].transcript.text,running.transcript.text)
            self.assertTrue(result.tasks[1]._core_pending_create)
            self.assertEqual(len(result.groups[0].members),2)


class GuiUnavailableExitTests(unittest.TestCase):
    setUpClass = frontend_fixture.FrontendFunctionalRepairTests.__dict__['setUpClass']
    setUp = frontend_fixture.FrontendFunctionalRepairTests.setUp
    tearDown = frontend_fixture.FrontendFunctionalRepairTests.tearDown

    def test_failed_core_can_exit_after_private_recovery_save(self):
        window = self.window
        window.cfg.confirm_exit_during_tasks = False
        window._task_queue_store = CoreTaskQueueStore(lambda *_: None,async_write=True)
        window._task_queue_recovery = GuiTaskRecoveryStore(self.root/'recovery'/'unsynced.json')
        task=window._task_queue_store.prepare(Task(1,'未同步输入','source.wav','file',state=TaskState.QUEUED))
        window.tasks=[task]
        window._task_queue_store_failed=True
        event=QCloseEvent()
        with patch.object(window,'_settle_registered_qthreads',return_value=True):
            window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        recovered,found=window._task_queue_recovery.merge(TaskQueueSnapshot((),()))
        self.assertTrue(found)
        self.assertEqual(recovered.tasks[0].source,'source.wav')
        self.assertEqual(recovered.tasks[0].state,TaskState.PENDING)

    def test_disk_failure_keeps_window_and_input(self):
        self.window.cfg.confirm_exit_during_tasks=False
        self.window._task_queue_store = CoreTaskQueueStore(lambda *_:None,async_write=True)
        self.window._task_queue_store_failed=True
        self.window._task_queue_recovery=Mock()
        self.window._task_queue_recovery.save.side_effect=OSError('disk full')
        event=QCloseEvent()
        with patch.object(self.window,'_settle_registered_qthreads',return_value=True):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
