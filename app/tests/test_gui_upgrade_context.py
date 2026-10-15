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
