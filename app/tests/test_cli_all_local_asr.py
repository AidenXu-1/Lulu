"""Local-only CLI integration; temporary ledgers, no model/network/Keychain."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.agent_cli.main import main
from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core.auth_guard import OpenProductAccessGuard
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from src.core.models import Transcript, Segment
from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_application import LocalFileTranscriptionApplicationService
from src.core.transcription_job_application import LocalTranscriptionJobApplication
from src.core.transcription_result_store import TranscriptionResultStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore

ENGINES = ('local_sensevoice', 'local_qwen3', 'local_whisper_mlx')


class AllLocalAsrTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='lulu-all-asr-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'input.wav'
        self.source.write_bytes(b'RIFF isolated input')
        support = self.root / 'support'
        support.mkdir(mode=0o700)
        layout = SharedProductStorageLayout.from_application_support_root(support)
        layout.product_root.mkdir(mode=0o700)
        self.store = JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore())
        self.results = TranscriptionResultStore(layout)
        self.calls = []
        self.probes = []
        guard = OpenProductAccessGuard()
        def ready(engine):
            self.probes.append(engine)
            return True
        self.application = LocalTranscriptionJobApplication(
            runtime_verification=dict(profile='trusted_test', verified=True,
                signature_kind='developer_id', installation_id='fixture'),
            authorization_guard=guard, model_readiness_probe=ready,
            transcription_service=LocalFileTranscriptionApplicationService(guard,
                pipeline_runner=self.pipeline, model_readiness_probe=ready),
            job_store=self.store, result_store=self.results,
            id_factory=lambda kind: kind + '-' + uuid4().hex,
            now=lambda: '2026-09-26T00:00:00Z')
        self.runtime = LocalTranscriptionCliRuntime(self.application,
            job_control=JobControlService(self.store), now=lambda: '2026-09-26T00:00:00Z')

    def pipeline(self, source, *, engine_name, progress, is_cancelled):
        self.calls.append(engine_name)
        return Transcript('完整转录正文。', [Segment('完整转录正文。', 0, 1)], 'zh', engine_name, source)

    def cli(self, *args):
        output, errors = StringIO(), StringIO()
        code = main(list(args) + ['--output', 'json'], stdout=output, stderr=errors,
                    local_transcription_runtime=self.runtime)
        self.assertEqual(errors.getvalue(), '')
        return code, json.loads(output.getvalue())

    def test_all_three_models_reach_selected_pipeline_and_result(self):
        jobs = []
        for engine in ENGINES:
            with self.subTest(engine=engine):
                code, prepared = self.cli('transcribe', 'prepare', str(self.source), '--engine', engine)
                self.assertEqual(code, 0, prepared)
                self.assertEqual(prepared['result']['engine'], engine)
                code, result = self.cli('transcribe', 'run', str(self.source), '--engine', engine)
                self.assertEqual(code, 0, result)
                jid = result['result']['job_receipt']['job_id']
                jobs.append(jid)
                self.assertEqual(self.store.get_job(jid).frozen_policy['engine'], engine)
                self.assertEqual(self.results.read(jid)['transcript']['engine'], engine)
        self.assertEqual(self.calls, list(ENGINES))
        self.assertEqual(len(set(jobs)), 3)
        self.assertEqual(set(self.probes), set(ENGINES))

    def test_unknown_cloud_duplicate_and_extra_options_do_not_execute(self):
        for tail in [('--engine', 'openai'), ('--engine', 'local_unknown'),
                     ('--engine', 'local_qwen3', '--engine', 'local_sensevoice'),
                     ('--secret', 'forbidden'), ('--engine',)]:
            code, result = self.cli('transcribe', 'run', str(self.source), *tail)
            self.assertNotEqual(code, 0, result)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.probes, [])

    def test_default_stays_sensevoice(self):
        code, result = self.cli('transcribe', 'run', str(self.source))
        self.assertEqual(code, 0, result)
        self.assertEqual(self.calls, ['local_sensevoice'])

    def test_result_read_preserves_full_unicode_without_repeating_inference(self):
        import base64
        body = '完整中文🙂' * 12000
        self.application._transcription_service._pipeline_runner = lambda source, **kw: Transcript(
            body, [], 'zh', kw['engine_name'], source)
        code, response = self.cli('transcribe', 'run', str(self.source))
        self.assertEqual(code, 0, response)
        jid = response['result']['job_receipt']['job_id']
        offset, chunks = 0, []
        while True:
            code, response = self.cli('jobs', 'result', '--job-id', jid, '--offset', str(offset))
            self.assertEqual(code, 0, response)
            result = response['result']
            chunk = base64.b64decode(result['data'], validate=True)
            self.assertGreater(len(chunk), 0)
            chunks.append(chunk)
            offset += len(chunk)
            if offset == result['total_bytes']:
                break
        self.assertEqual(json.loads(b''.join(chunks))['transcript']['text'], body)

    def test_status_and_missing_model_use_existing_installed_model_state(self):
        from src.core import models_manager as mm
        with patch.object(mm, 'is_downloaded', return_value=False), \
             patch.object(mm, 'model_path', return_value=self.root / 'missing'), \
             patch.object(mm, 'runtime_issue', return_value=None):
            code, value = self.cli('transcribe', 'status')
            self.assertEqual(code, 0, value)
            self.assertEqual({m['engine'] for m in value['result']['models']}, set(ENGINES))
            self.assertTrue(all(m['state'] == 'not_installed' for m in value['result']['models']))

    def test_model_failures_are_distinct_and_never_create_jobs(self):
        from src.core import models_manager as mm
        from src.core.local_transcription_models import probe_local_model_readiness
        self.application._model_readiness_probe = probe_local_model_readiness
        incomplete = self.root / 'incomplete'
        incomplete.mkdir()
        (incomplete / 'partial-weight').write_bytes(b'incomplete')
        cases = [
            (False, self.root / 'missing', None, 'not_installed', 'MODEL_NOT_INSTALLED', '未安装'),
            (False, incomplete, None, 'needs_repair', 'MODEL_INSTALLATION_INCOMPLETE', '不完整'),
            (True, incomplete, '运行依赖缺失', 'runtime_unavailable', 'MODEL_RUNTIME_UNAVAILABLE', '运行环境'),
        ]
        for engine in ENGINES:
            for downloaded, path, issue, state, reason, message in cases:
                with self.subTest(engine=engine, state=state), \
                     patch.object(mm, 'is_downloaded', return_value=downloaded), \
                     patch.object(mm, 'model_path', return_value=path), \
                     patch.object(mm, 'runtime_issue', return_value=issue):
                    code, value = self.cli('transcribe', 'status', '--engine', engine)
                    self.assertEqual(code, 0)
                    self.assertEqual(value['result']['models'][0]['state'], state)
                    code, value = self.cli('transcribe', 'run', str(self.source), '--engine', engine)
                    self.assertEqual(code, 30, value)
                    self.assertEqual(value['error']['details']['reason_code'], reason)
                    self.assertIn(message, value['error']['message'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.list_jobs(), ())

    def test_default_adapter_reaches_registry_for_each_model_offline(self):
        from src.core import pipeline
        from src.core.engines import registry
        from src.core.transcription_application import run_local_transcription_pipeline_adapter
        class Engine:
            display_name = 'isolated engine'
            def __init__(self, name): self.name = name
            def transcribe(self, path, progress, is_cancelled):
                return Transcript('实际分派结果', [Segment('实际分派结果', 0, 1)], 'zh', self.name)
        self.application._transcription_service._pipeline_runner = run_local_transcription_pipeline_adapter
        with patch.dict(registry._local_instances, {}, clear=True), \
             patch.object(registry, 'runtime_issue', return_value=None), \
             patch.object(registry, 'is_ready', return_value=True), \
             patch.object(registry, '_build_local', side_effect=lambda name, runtime: Engine(name)) as build, \
             patch.object(pipeline, 'to_wav_16k_mono', return_value=self.source), \
             patch('src.core.engines.registry.secrets.get_config', side_effect=AssertionError('no cloud')):
            for engine in ENGINES:
                code, value = self.cli('transcribe', 'run', str(self.source), '--engine', engine)
                self.assertEqual(code, 0, value)
                jid = value['result']['job_receipt']['job_id']
                self.assertEqual(self.results.read(jid)['transcript']['engine'], engine)
            self.assertEqual([c.args[0] for c in build.call_args_list], list(ENGINES))

    def test_wrong_model_result_is_rejected(self):
        self.application._transcription_service._pipeline_runner = lambda source, **kw: Transcript(
            '错误模型', [], 'zh', 'local_sensevoice', source)
        code, value = self.cli('transcribe', 'run', str(self.source), '--engine', 'local_qwen3')
        self.assertNotEqual(code, 0)
        self.assertEqual(value['error']['code'], 'INTERNAL_ERROR')
        self.assertTrue(all(job.state.value == 'failed' for job in self.store.list_jobs()))

    def test_results_reject_invalid_offsets_and_tampering(self):
        code, value = self.cli('transcribe', 'run', str(self.source))
        jid = value['result']['job_receipt']['job_id']
        for offset in ['-1', 'x', '99999999', '999999999999999999999999']:
            code, value = self.cli('jobs', 'result', '--job-id', jid, '--offset', offset)
            self.assertEqual(code, 2, value)
        path = self.results.directory / (jid + '.json')
        path.write_bytes(path.read_bytes().replace('完整'.encode(), '更改'.encode()))
        code, value = self.cli('jobs', 'result', '--job-id', jid, '--offset', '0')
        self.assertEqual(code, 60, value)
        self.assertEqual(self.calls, ['local_sensevoice'])

    def test_unverified_status_does_not_read_model_files(self):
        from src.core import models_manager as mm
        self.application._runtime_verification['verified'] = False
        with patch.object(mm, 'is_downloaded', side_effect=AssertionError('must not inspect')):
            code, value = self.cli('transcribe', 'status')
        self.assertEqual(code, 30, value)
        self.assertEqual(value['error']['details']['reason_code'], 'PACKAGE_TRUST_EVIDENCE_UNAVAILABLE')

    def test_status_ready_and_check_failure_are_not_confused_with_missing(self):
        from src.core import models_manager as mm
        with patch.object(mm, 'is_downloaded', return_value=True), \
             patch.object(mm, 'runtime_issue', return_value=None), \
             patch('src.core.local_transcription_models.find_spec', return_value=object()):
            code, value = self.cli('transcribe', 'status')
            self.assertEqual(code, 0)
            self.assertTrue(all(m['ready'] and m['installed'] for m in value['result']['models']))
        with patch.object(mm, 'is_downloaded', side_effect=OSError('unavailable volume')):
            code, value = self.cli('transcribe', 'status')
            self.assertEqual(code, 0)
            self.assertTrue(all(m['state'] == 'check_failed' and m['installed'] is None
                                for m in value['result']['models']))
        self.assertEqual(self.calls, [])

    def test_model_disappearing_after_preflight_returns_readiness_error(self):
        from src.core.local_transcription_models import LocalModelUnavailable
        def missing(engine):
            raise LocalModelUnavailable('MODEL_NOT_READY', 'MODEL_NOT_INSTALLED')
        self.application._transcription_service._model_readiness_probe = missing
        code, value = self.cli('transcribe', 'run', str(self.source), '--engine', 'local_qwen3')
        self.assertEqual(code, 30, value)
        self.assertEqual(value['error']['details']['reason_code'], 'MODEL_NOT_INSTALLED')
        self.assertEqual(self.calls, [])
        self.assertEqual([job.state.value for job in self.store.list_jobs()], ['failed'])


class WorkspaceAllAsrTests(unittest.TestCase):
    from tests.test_agent_workspace_media import MediaTests as _Fixture
    join_workers = _Fixture.join_workers
    wait = _Fixture.wait
    start = _Fixture.start

    def setUp(self):
        self._Fixture.setUp(self)
        self.engine = 'local_qwen3'

    def pipeline(self, source, *, engine_name, progress, is_cancelled):
        self.calls.append(engine_name)
        self.assertEqual(engine_name, self.engine)
        progress('识别中', .6)
        return Transcript('转录结果。', [Segment('转录结果。', 0, 1)], 'zh', engine_name, source)

    def plan(self, request='fixture'):
        return self.service.prepare_media(self.chat['id'], request, str(self.source), self.engine)

    def command(self, verb, request):
        path = self.root / 'request.json'
        path.write_text(json.dumps(request))
        output, errors = StringIO(), StringIO()
        code = main(['workspace', 'tasks', verb, '--request', str(path), '--output', 'json'],
                    local_transcription_runtime=self.runtime, stdout=output, stderr=errors)
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0, value)
        return value['result']

    def test_cli_workspace_all_models_save_readable_documents(self):
        for engine in ENGINES:
            self.engine = engine
            plan = self.command('prepare', dict(conversation_id=self.chat['id'], request_id=engine,
                source_path=str(self.source), engine=engine))
            self.assertEqual(plan['engine'], engine)
            job = self.command('start', dict(plan_id=plan['plan_id'], expected_hash=plan['plan_hash']))
            result = self.wait(job['job_id'])
            self.assertEqual(result['state'], 'succeeded')
            self.assertIn('转录结果', self.service.snapshot(result['document_id'])['text'])
            self.assertEqual(self.control.show(job['job_id']).frozen_policy['engine'], engine)
        self.assertEqual(self.calls, list(ENGINES))

    def test_qwen_cancellation_prevents_late_document_write(self):
        self._Fixture.test_cancel_via_common_jobs_entry_blocks_late_result_and_document(self)

    def test_workspace_preserves_missing_model_error_without_saving_plan(self):
        from src.core.local_transcription_models import LocalModelUnavailable
        def missing(engine):
            raise LocalModelUnavailable('MODEL_NOT_READY', 'MODEL_NOT_INSTALLED')
        self.application._model_readiness_probe = missing
        path = self.root / 'request.json'
        path.write_text(json.dumps(dict(conversation_id=self.chat['id'], request_id='missing',
            source_path=str(self.source), engine='local_qwen3')))
        output = StringIO()
        code = main(['workspace', 'tasks', 'prepare', '--request', str(path), '--output', 'json'],
                    local_transcription_runtime=self.runtime, stdout=output)
        value = json.loads(output.getvalue())
        self.assertEqual(code, 30, value)
        self.assertEqual(value['error']['details']['reason_code'], 'MODEL_NOT_INSTALLED')
        self.assertIn('未安装', value['error']['message'])
        self.assertEqual(self.calls, [])

    def test_whisper_saved_result_recovery_does_not_repeat_inference(self):
        self.engine = 'local_whisper_mlx'
        self._Fixture.test_saved_result_recovery_does_not_infer_or_duplicate_even_after_edit(self)
