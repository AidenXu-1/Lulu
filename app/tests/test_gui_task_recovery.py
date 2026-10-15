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


class GuiUnavailableExitTests(unittest.TestCase):
    setUpClass = frontend_fixture.FrontendFunctionalRepairTests.__dict__['setUpClass']
    setUp = frontend_fixture.FrontendFunctionalRepairTests.setUp
    tearDown = frontend_fixture.FrontendFunctionalRepairTests.tearDown
