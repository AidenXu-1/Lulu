"""Legacy queued settings can be filled once without weakening frozen policy."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.job_ledger.contracts import JobLedgerError
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.task import Task, TaskExecutionPolicy, TaskState
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from tests.test_shared_core_gui_task_ledger import _runtime, _dispatcher


class LegacyQueuedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = JobLedgerStore(Path(self.tmp.name).resolve() / 'jobs', FakeIntegrityAnchorStore())
        self.control = JobControlService(self.ledger)
        self.store = CoreTaskQueueStore(_dispatcher(_runtime(self.control, [f'job-{i}' for i in range(20)])))

    def queued(self, *, single_link=False, prefer_subtitles=None):
        policy = TaskExecutionPolicy(origin='homepage_batch_transcribe', mode='transcription_only',
            stable_key='video:1', material_snapshot=MaterialPreferenceSnapshot('audio', False, False),
            media_destination='temporary_only', prefer_subtitles=prefer_subtitles)
        if single_link:
            policy = replace(policy, origin='single_link', mode='materialize', media_destination='user_directory')
        task = self.store.register(Task(1, '旧批次', 'https://example.invalid/1', 'link',
            stable_key='video:1', execution_policy=policy))
        task.state = TaskState.QUEUED
        self.store.save([task], [])
        return task

    def test_old_queued_empty_settings_can_bind_once_before_running(self):
        task = self.queued()
        task.execution_policy = replace(task.execution_policy, engine_name='local_sensevoice', prefer_subtitles=False)
        task.state = TaskState.RUNNING
        self.assertTrue(self.store.save([task], []))
        self.assertEqual(self.control.show(task.id).state.value, 'running')
        self.assertEqual(self.control.show(task.id).frozen_policy['gui_execution_policy']['engine_name'], 'local_sensevoice')

    def test_queued_binding_rejects_changes_to_admitted_input_and_outputs(self):
        changes = ({'stable_key':'different'}, {'media_directory':'/tmp/elsewhere'},
                   {'media_destination':'none'}, {'requested_outputs':('text',)},
                   {'material_snapshot':MaterialPreferenceSnapshot('video', True, False)},
                   {'origin':'homepage_batch_transcribe', 'mode':'transcription_only', 'media_destination':'temporary_only'})
        for changed in changes:
            with self.subTest(changed=changed):
                task = self.queued(single_link=True)
                before = deepcopy(self.control.show(task.id))
                task.execution_policy = replace(task.execution_policy, engine_name='local_sensevoice', prefer_subtitles=False, **changed)
                task.state = TaskState.RUNNING
                with self.assertRaises(JobLedgerError):
                    self.store.save([task], [])
                self.assertEqual(self.control.show(task.id), before)

    def test_filling_missing_model_cannot_change_an_existing_subtitle_setting(self):
        task = self.queued(prefer_subtitles=False)
        before = deepcopy(self.control.show(task.id))
        task.execution_policy = replace(task.execution_policy, engine_name='local_sensevoice', prefer_subtitles=True)
        task.state = TaskState.RUNNING
        with self.assertRaises(JobLedgerError):
            self.store.save([task], [])
        self.assertEqual(self.control.show(task.id), before)

    def test_bound_model_and_subtitle_values_cannot_change(self):
        for changed in ({'engine_name':'local_whisper_mlx'}, {'prefer_subtitles':True}):
            with self.subTest(changed=changed):
                task = self.queued()
                task.execution_policy = replace(task.execution_policy, engine_name='local_sensevoice', prefer_subtitles=False)
                task.state = TaskState.RUNNING
                self.store.save([task], [])
                before = deepcopy(self.control.show(task.id))
                task.execution_policy = replace(task.execution_policy, **changed)
                from src.core.gui_task_ledger import _execution_policy
                with self.assertRaises(JobLedgerError):
                    self.ledger.freeze_gui_policy(task.id, expected_revision=before.job_revision,
                        policy=_execution_policy(task.execution_policy), changed_at='2026-09-25T00:00:00Z')
                self.assertEqual(self.control.show(task.id), before)
