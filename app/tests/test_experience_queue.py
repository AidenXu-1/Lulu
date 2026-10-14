"""Queue session boundary; no service, user files or network."""
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_task_queue_ui_recovery import TaskQueueUiRecoveryTests
from src.core.task import Task, TaskState, TaskGroup, TaskGroupMember
from src.core.task_queue_store import TaskQueueSnapshot

class ExperienceQueueTests(TaskQueueUiRecoveryTests):
    def test_core_completed_hidden_but_failed_and_pending_retained(self):
        window = self._window()
        window._task_queue_store = SimpleNamespace(authoritative=True)
        del window._session_hidden_completed
        tasks = (Task(10,'完成','/tmp/a','file',state=TaskState.DONE),
                 Task(11,'失败','/tmp/b','file',state=TaskState.FAILED,error='测试失败'),
                 Task(12,'排队','/tmp/c','file',state=TaskState.QUEUED))
        group = TaskGroup(1,'混合组',[TaskGroupMember(str(t.id),t.title,t.source,t.kind,task_id=t.id) for t in tasks])
        window._restore_task_queue(TaskQueueSnapshot(tasks,(group,)))
        self.assertEqual([t.id for t in window.tasks],[11,12])
        self.assertEqual([m.task_id for m in window.task_groups[0].members],[11,12])
        self.assertEqual(tasks[0].state,TaskState.DONE)
        self.assertEqual(window._next_id,13)

    def test_noop_card_refresh_does_not_restyle(self):
        window = self._window()
        workspace = window.extraction_workspace
        workspace.ready = True
        workspace._models = [{"id": "fixture", "ready": True}]
        import time
        workspace._models_at = time.monotonic()
        received = []
        workspace.bridge.snapshot.connect(received.append)
        task = Task(1, "失败", "/tmp/a", "file", state=TaskState.FAILED, error="原因")
        window.tasks.append(task)
        workspace.publish()
        workspace.publish()
        self.assertEqual(len(received), 1)
        task.state = TaskState.PENDING
        workspace.publish()
        self.assertEqual(len(received), 2)

    def test_retired_live_page_returns_to_extraction(self):
        window=self._window()
        window._switch_page(8)
        self.assertEqual(window.stack.currentIndex(),0)
        self.assertFalse(hasattr(window, "live_clips_page"))

    def test_completed_this_session_survives_poll_but_not_reopen(self):
        window=self._window();window._task_queue_store=SimpleNamespace(authoritative=True)
        del window._session_hidden_completed
        def projected(state):
            task=Task(21,'本次任务','/tmp/new.wav','file',state=state)
            task._core_state='prepared' if state==TaskState.PENDING else 'succeeded'
            task._core_job_revision=1;task._core_result_ref=None;task._core_view_only=True
            return task
        window._restore_task_queue(TaskQueueSnapshot((projected(TaskState.PENDING),),()))
        window._restore_task_queue(TaskQueueSnapshot((projected(TaskState.DONE),),()))
        self.assertEqual(window.tasks[0].state,TaskState.DONE)
        window._restore_task_queue(TaskQueueSnapshot((projected(TaskState.DONE),),()))
        self.assertEqual(len(window.tasks),1)
        reopened=self._window();reopened._task_queue_store=SimpleNamespace(authoritative=True)
        del reopened._session_hidden_completed
        snapshot=TaskQueueSnapshot((projected(TaskState.DONE),),())
        reopened._restore_task_queue(snapshot)
        reopened._restore_task_queue(snapshot)
        self.assertEqual(reopened.tasks,[])
        self.assertEqual(snapshot.tasks[0].state,TaskState.DONE)
