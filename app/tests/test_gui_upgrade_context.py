import base64
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.core.agent_entry.task_handover import seal_handover, open_handover, gui_history
from src.core.agent_entry.task_handover_context import capture_context, validate_context
from src.core.gui_task_ledger import CoreTaskQueueStore, _task_from_job
from src.core.task import Task, TaskGroup, TaskGroupMember, TaskState
from tests import test_frontend_functional_repair as fixture
from tests.test_task_handover import record


def context(row):
    return {'tasks': {row['job_id']: {'title': '原始作品标题', 'source': 'https://www.douyin.com/video/100',
        'kind': 'link', 'folder_id': 'folder', 'stable_key': 'douyin:100', 'execution_policy': None}},
        'view': {'groups': [{'id': 1, 'name': '原始分组', 'folder_id': 'folder', 'expanded': True,
            'members': [{'task_id': row['job_id']}]}], 'hidden_ids': []}}


class GuiUpgradeContextTests(unittest.TestCase):
    def test_live_folder_registration_is_sealed_without_creating_a_transcription_card(self):
        row = dict(record(), job_kind='live_recording', result_ref=None)
        policy = dict(recording_id='recording-'+'1'*32, host_id='host-'+'2'*32,
                      directory='/synthetic/recording-'+'1'*32)
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            ledger = root/'task-generations'/('a'*40)/'jobs/job-ledger-v1/snapshot.json'
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({'jobs': [dict(row, created_by='gui', frozen_policy=policy)]}))
            ledger.chmod(0o600)
            value = capture_context(root, root/'app', [row], generation='a'*40)
        self.assertEqual(value['tasks'], {})
        self.assertEqual(value['live_recordings'], {row['job_id']: policy})
        owner = dict(installation_id='test', installation_id_digest='b'*64, component_manifest_sha256='c'*64)
        keys = SimpleNamespace(get_text=lambda _: base64.b64encode(b'x'*32).decode())
        sealed = seal_handover([row], keys=keys, owner=owner, previous_manifest='d'*64,
                               product_root=Path('/test'), gui_context=value)
        self.assertEqual(open_handover(sealed, keys=keys, owner=owner, product_root=Path('/test')), [row])
        sealed['body']['gui_context']['live_recordings'][row['job_id']]['directory'] = '/another/folder'
        with self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
            open_handover(sealed, keys=keys, owner=owner, product_root=Path('/test'))
        with self.assertRaisesRegex(RuntimeError, 'GUI_HANDOVER_INVALID'):
            validate_context(value, [dict(row, job_kind='local_transcription')])

    def test_live_registration_survives_multiple_upgrades_and_conflicts_are_rejected(self):
        from src.core.agent_entry.standalone_history import merge_history
        row = dict(record(), job_kind='live_recording', result_ref=None)
        policy = dict(recording_id='recording-'+'1'*32, host_id='host-'+'2'*32,
                      directory='/synthetic/recording-'+'1'*32)
        older = {'tasks': {}, 'view': {'groups': [], 'hidden_ids': []},
                 'live_recordings': {row['job_id']: policy}}
        newer = {'tasks': {}, 'view': {'groups': [], 'hidden_ids': []}}
        rows, merged = merge_history(([row], older), ([row], newer))
        self.assertEqual(merged['live_recordings'], older['live_recordings'])
        changed = deepcopy(older)
        changed['live_recordings'][row['job_id']]['directory'] = '/different/folder'
        with self.assertRaisesRegex(RuntimeError, 'GUI_HANDOVER_INVALID'):
            merge_history(([row], older), ([row], changed))

    def test_runtime_passes_only_verified_live_registration_to_recording_service(self):
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import LocalTranscriptionJobApplication
        row = dict(record(), job_kind='live_recording', result_ref=None)
        policy = dict(recording_id='recording-'+'1'*32, host_id='host-'+'2'*32,
                      directory='/synthetic/recording-'+'1'*32)
        value = {'tasks': {}, 'view': {'groups': [], 'hidden_ids': []},
                 'live_recordings': {row['job_id']: policy}}
        runtime = LocalTranscriptionCliRuntime(Mock(spec=LocalTranscriptionJobApplication),
            historical_jobs=[row], historical_context=value)
        runtime._authorized_job_control = Mock()
        with patch('src.core.live_recording.service.LiveService') as service:
            runtime.live_recording()
            self.assertEqual(service.call_args.kwargs['historical_recordings'], value['live_recordings'])

    def test_display_input_is_sealed_but_history_stays_read_only(self):
        row = record()
        owner = {'installation_id': 'test', 'installation_id_digest': 'b'*64, 'component_manifest_sha256': 'c'*64}
        keys = SimpleNamespace(get_text=lambda _: base64.b64encode(b'x'*32).decode())
        value = seal_handover([row], keys=keys, owner=owner, previous_manifest='d'*64,
            product_root=Path('/test'), gui_context=context(row))
        self.assertEqual(open_handover(value, keys=keys, owner=owner, product_root=Path('/test')), [row])
        projected = _task_from_job(gui_history(row, value['body']['gui_context']['tasks'][row['job_id']]))
        self.assertEqual(projected.title, '原始作品标题')
        self.assertTrue(projected._core_view_only)
        value['body']['gui_context']['tasks'][row['job_id']]['source'] = 'changed'
        with self.assertRaisesRegex(RuntimeError, 'HANDOVER_INVALID'):
            open_handover(value, keys=keys, owner=owner, product_root=Path('/test'))

    def test_capture_keeps_matching_source_group_and_ignores_stale_hidden_ids(self):
        row = dict(record(), job_kind='gui_transcription'); gui = context(row)
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            jobs = root / 'task-generations' / ('a'*40) / 'jobs'
            ledger = jobs / 'job-ledger-v1/snapshot.json'; ledger.parent.mkdir(parents=True)
            raw = {'jobs': [dict(row, created_by='gui', frozen_policy={'gui_task': gui['tasks'][row['job_id']]})]}
            ledger.write_text(json.dumps(raw)); ledger.chmod(0o600)
            view = jobs / 'gui-task-view-v1.json'
            view.write_text(json.dumps(dict(gui['view'], hidden_ids=['older-gone']))); view.chmod(0o600)
            self.assertEqual(capture_context(root, root/'app', [row], generation='a'*40), gui)
            raw['jobs'][0]['job_revision'] += 1
            ledger.write_text(json.dumps(raw))
            with self.assertRaisesRegex(RuntimeError, 'TASKS_CHANGED'):
                capture_context(root, root/'app', [row], generation='a'*40)

    def test_workspace_conversation_without_gui_task_metadata_is_not_a_transcription_card(self):
        row = record()
        row.update(job_id='workspace-job-example', job_kind='workspace_conversation',
                   result_ref='workspace-result:sha256:' + 'a' * 64,
                   receipt_ref='lulu-job:workspace-job-example:revision:4')
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            jobs = root / 'task-generations' / ('a'*40) / 'jobs'
            ledger = jobs / 'job-ledger-v1/snapshot.json'; ledger.parent.mkdir(parents=True)
            raw = {'jobs': [dict(row, created_by='gui', frozen_policy={})]}
            ledger.write_text(json.dumps(raw)); ledger.chmod(0o600)
            self.assertEqual(
                capture_context(root, root/'app', [row], generation='a'*40),
                {'tasks': {}, 'view': {'groups': [], 'hidden_ids': []}},
            )

    def test_hiding_history_survives_reload_and_preserves_original_job(self):
        row=record();job=gui_history(row,context(row)['tasks'][row['job_id']])
        original=deepcopy(job)
        view={'revision':0,'groups':[],'hidden_ids':[]}
        def dispatch(command,args):
            if command=='gui.jobs.list':return {'jobs':[deepcopy(job)],'view':deepcopy(view)}
            if command=='gui.jobs.view':
                self.assertEqual(args['expected_revision'],view['revision'])
                view.update(groups=args['groups'],hidden_ids=args['hidden_ids'],revision=view['revision']+1)
                return deepcopy(view)
            self.fail('Removing history must not mutate its job: '+command)
        store=CoreTaskQueueStore(dispatch,async_write=True)
        self.assertEqual(len(store.load().tasks),1)
        self.assertTrue(store.save([],[]))
        self.assertEqual(CoreTaskQueueStore(dispatch,async_write=True).load().tasks,())
        self.assertEqual(job,original)
        self.assertEqual(view['hidden_ids'],[row['job_id']])

    def test_unknown_job_in_context_is_rejected(self):
        row = record(); value = context(row)
        value['view']['groups'][0]['members'].append({'task_id': 'other'})
        with self.assertRaisesRegex(RuntimeError, 'GUI_HANDOVER_INVALID'):
            validate_context(value, [row])


class GuiHistoryRetryTests(unittest.TestCase):
    setUpClass = fixture.FrontendFunctionalRepairTests.__dict__['setUpClass']
    setUp = fixture.FrontendFunctionalRepairTests.setUp
    tearDown = fixture.FrontendFunctionalRepairTests.tearDown

    def test_removing_history_persists_when_only_readonly_cards_remain(self):
        window=self.window
        window._task_queue_store=CoreTaskQueueStore(lambda *_:None,async_write=True)
        window._task_queue_startup_pending=False
        window._task_queue_snapshot_locked=False
        window._task_queue_store_failed=False
        window._core_save_worker=None
        window._core_hidden_intents=set()
        window.tasks=[Task('old-'+str(i),'历史转录','','file',state=TaskState.FAILED) for i in range(2)]
        for task in window.tasks:task._core_view_only=True
        window.task_groups=[]
        worker=Mock()
        with patch('src.ui.worker.CoreTaskSaveWorker',return_value=worker):
            window._delete_task('old-0')
        worker.start.assert_called_once()
        self.assertEqual(worker.hidden_intents,{'old-0'})
        self.assertEqual([t.id for t in window.tasks],['old-1'])
        window._core_save_worker=None

    def test_group_retry_creates_new_pending_input_and_preserves_success_and_old_receipt(self):
        window = self.window
        window._task_queue_store = CoreTaskQueueStore(lambda *_:None, async_write=True)
        failed = Task('old-failed', '失败作品', 'https://www.douyin.com/video/100', 'link',
            state=TaskState.FAILED, stable_key='douyin:100')
        failed._core_view_only = True
        success = Task('old-success', '成功作品', 'https://www.douyin.com/video/200', 'link', state=TaskState.DONE)
        success._core_view_only = True
        window.tasks = [failed, success]
        group = TaskGroup(1, '原始组', [TaskGroupMember(t.stable_key,t.title,t.source,t.kind,t.id) for t in window.tasks])
        window.task_groups = [group]
        with patch.object(window, '_persist_task_queue', return_value=True), patch.object(window, '_pump') as pump:
            window._retry_task_group(1)
        new = window._task(group.members[0].task_id)
        self.assertNotEqual(new.id, failed.id)
        self.assertEqual(new.source, failed.source)
        self.assertEqual(new.state, TaskState.PENDING)
        self.assertTrue(new._core_pending_create)
        self.assertFalse(getattr(new, '_core_view_only', False))
        self.assertEqual(failed.state, TaskState.FAILED)
        self.assertEqual(group.members[1].task_id, success.id)
        self.assertIs(window._task(new.id), new)
        self.assertEqual(len(window.extraction_workspace.rows()), 2)
        pump.assert_not_called()
