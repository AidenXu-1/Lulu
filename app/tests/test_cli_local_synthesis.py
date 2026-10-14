"""Local-only adapter tests: fake authorization/model, real ledger and WAV files."""
from io import StringIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
import uuid
import wave

from src.agent_cli.main import main
from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core.auth_guard import UnifiedLicenseAuthGuard
from src.core.job_ledger.store import JobLedgerStore
from src.core.job_ledger.service import JobControlService
from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_result_store import TranscriptionResultStore
from src.core.transcription_job_application import LocalTranscriptionJobApplication
from src.core.synthesis.base import VoiceProfile
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore


class LocalSynthesisTests(unittest.TestCase):
    def setUp(self):
        from src.core.local_synthesis_job_application import LocalSynthesisJobApplication
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.text = self.root / 'text.txt'
        self.text.write_text('这是一条本地配音测试。')
        self.reference = self.root / 'reference.wav'
        self.wav(self.reference)
        self.voice = VoiceProfile('voice-one', '已保存音色', str(self.reference), '参考文本')
        self.store = JobLedgerStore(self.root / 'ledger', FakeIntegrityAnchorStore())
        self.control = JobControlService(self.store, enforce_fencing=True)
        self.results = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(self.root))
        self.license = SimpleNamespace(cached_status_projection=lambda: SimpleNamespace(allowed=True, code='cached_allowed'))
        self.application = LocalTranscriptionJobApplication(
            runtime_verification=dict(profile='trusted_test', verified=True, signature_kind='developer_id', installation_id='installation-001'),
            authorization_guard=UnifiedLicenseAuthGuard(self.license), model_readiness_probe=lambda _: True)
        self.entered, self.release = Event(), Event()
        self.addCleanup(self.release.set)
        self.ready = 'ready'
        self.fail = False
        self.calls = []
        def pipeline(**kwargs):
            self.calls.append(kwargs)
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError('test timed out')
            if self.fail:
                raise RuntimeError('private engine detail')
            kwargs['progress']('untrusted model text', .5)
            self.wav(kwargs['output_path'])
        self.service = LocalSynthesisJobApplication(application=self.application,
            job_store=self.store, result_store=self.results,
            now=lambda: '2026-09-08T12:00:00Z', id_factory=lambda prefix: prefix+'-'+uuid.uuid4().hex,
            readiness=lambda engine, options: self.ready, voices=lambda: [self.voice], pipeline=pipeline, resource_busy=lambda: False)
        self.runtime = LocalTranscriptionCliRuntime(self.application, job_control=self.control,
            now=lambda: '2026-09-08T12:00:00Z', local_synthesis_service=self.service)

    @staticmethod
    def wav(path):
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(b'\0\0' * 1600)

    def invoke(self, args):
        out, err = StringIO(), StringIO()
        code = main(args, stdout=out, stderr=err, local_transcription_runtime=self.runtime)
        self.assertEqual(err.getvalue(), '')
        return code, json.loads(out.getvalue())

    def start(self, name='new.wav'):
        return self.invoke(['tts', 'synthesize', '--engine', 'local_voxcpm2', '--text-file', str(self.text),
            '--voice-id', self.voice.id, '--destination', str(self.root/name), '--output', 'json'])

    def finish(self, jid):
        self.release.set()
        self.service.wait_for_worker(jid, timeout=5)
        return self.runtime.jobs_receipt(jid)

    def test_read_only_status_and_activation(self):
        before = set(self.root.rglob('*'))
        code, result = self.invoke(['tts', 'status', '--output', 'json'])
        self.assertEqual(code, 0)
        self.assertEqual(result['result']['runtime_state'], 'ready')
        self.assertEqual(result['result']['voices'][0]['voice_id'], 'voice-one')
        self.assertEqual(before, set(self.root.rglob('*')))
        self.assertTrue({'TTS-01', 'TTS-02'} <= self.runtime.activated_capability_ids)

    def test_async_job_verified_audio_and_gui_separation(self):
        code, result = self.start()
        self.assertEqual(code, 0)
        jid = result['result']['job_id']
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.control.show(jid).state.value, 'running')
        self.assertEqual(self.runtime.gui_jobs_list()['jobs'], [])
        receipt = self.finish(jid)
        self.assertEqual(receipt['state'], 'succeeded')
        self.assertEqual(receipt['audio']['duration_seconds'], .1)
        self.assertEqual(receipt['audio']['sha256'], hashlib.sha256((self.root/'new.wav').read_bytes()).hexdigest())
        self.assertEqual(len(self.runtime.jobs_list()['jobs']), 1)

    def test_cancel_does_not_publish(self):
        _, result = self.start()
        jid = result['result']['job_id']
        self.runtime.jobs_cancel(jid)
        self.assertEqual(self.finish(jid)['state'], 'cancelled')
        self.assertFalse((self.root/'new.wav').exists())

    def test_busy_does_not_create_another_job(self):
        _, result = self.start()
        code, error = self.start('another.wav')
        self.assertEqual(error['error']['code'], 'RESOURCE_BUSY')
        self.assertEqual(len(self.control.list()), 1)
        self.finish(result['result']['job_id'])

    def test_existing_destination_and_late_collision_never_overwritten(self):
        target = self.root/'new.wav'
        target.write_bytes(b'existing')
        self.assertNotEqual(self.start()[0], 0)
        self.assertEqual(len(self.control.list()), 0)
        target.unlink()
        _, result = self.start()
        target.write_bytes(b'created while worker was running')
        self.assertEqual(self.finish(result['result']['job_id'])['state'], 'failed')
        self.assertEqual(target.read_bytes(), b'created while worker was running')

    def test_not_ready_and_unknown_options_have_no_jobs(self):
        self.ready = 'runtime_only'
        self.assertEqual(self.start()[1]['error']['code'], 'RUNTIME_NOT_READY')
        self.assertNotEqual(self.invoke(['tts', 'status', '--download', '--output', 'json'])[0], 0)
        self.assertEqual(len(self.control.list()), 0)

    def test_failure_is_durable_and_does_not_leak_engine_text(self):
        self.fail = True
        _, result = self.start()
        receipt = self.finish(result['result']['job_id'])
        self.assertEqual(receipt['state'], 'failed')
        self.assertNotIn('private engine detail', json.dumps(receipt))
        self.assertFalse((self.root/'new.wav').exists())

    def test_unsafe_source_and_oversize_text_create_no_job(self):
        original = self.text
        self.text = self.root / 'link.txt'
        self.text.symlink_to(original)
        self.assertEqual(self.start()[1]['error']['code'], 'INVALID_ARGUMENT')
        self.text = original
        self.text.write_text('字' * 2001)
        self.assertEqual(self.start()[1]['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(len(self.control.list()), 0)

    def test_denied_authorization_precedes_input_read(self):
        self.license.cached_status_projection = lambda: SimpleNamespace(allowed=False, code='expired')
        self.text.unlink()
        self.assertEqual(self.start()[1]['error']['code'], 'AUTH_REQUIRED')
        self.assertEqual(len(self.control.list()), 0)

    def test_audio_survives_handover_and_rejects_tampering(self):
        from src.core.agent_entry.task_handover import capture_terminal_tasks
        _, result = self.start()
        jid = result['result']['job_id']
        receipt = self.finish(jid)
        def cli(args):
            return self.invoke(args)[1]['result']
        rows = capture_terminal_tasks(cli)
        self.assertNotIn('audio', rows[0])
        runtime = LocalTranscriptionCliRuntime(self.application, job_control=self.control,
            historical_jobs=rows, local_synthesis_service=self.service)
        self.assertEqual(runtime.jobs_receipt(jid)['audio'], receipt['audio'])
        target = self.results.directory / (jid+'.json')
        target.write_bytes(target.read_bytes().replace(b'16000', b'24000'))
        with self.assertRaisesRegex(Exception, 'INTEGRITY_ERROR'):
            runtime.jobs_receipt(jid)

    def test_worker_lost_never_publishes_or_replays(self):
        _, result = self.start()
        jid = result['result']['job_id']
        self.assertTrue(self.entered.wait(2))
        self.store.recover_orphaned_running(changed_at='2026-09-08T12:01:00Z')
        self.finish(jid)
        self.assertEqual(self.control.show(jid).state.value, 'recovery_required')
        self.assertFalse((self.root/'new.wav').exists())

    def test_progress_is_bounded_and_contains_no_model_message(self):
        _, result = self.start()
        jid = result['result']['job_id']
        self.finish(jid)
        events = self.control.wait(jid)
        self.assertIn('tts.progress.50', [e.event_type for e in events])
        self.assertNotIn('untrusted model text', repr(events))

    def test_all_four_model_routes_options_progress_and_audio(self):
        from src.core.synthesis.cli_options import MODELS
        code, status = self.invoke(['tts', 'status', '--output', 'json'])
        self.assertEqual(code, 0)
        self.assertEqual({v['engine'] for v in status['result']['models']}, set(MODELS))
        self.release.set()
        for engine, (_, pipeline_engine, version) in MODELS.items():
            with self.subTest(engine=engine):
                opts = {'emotion_mode': 'template', 'emotion_preset': 'gentle',
                        'generation_options': {'top_k': 40}} if version else {'seed': 7}
                file = self.root / (engine+'.json')
                file.write_text(json.dumps(opts))
                code, result = self.invoke(['tts', 'synthesize', '--engine', engine, '--text-file', str(self.text),
                    '--voice-id', self.voice.id, '--destination', str(self.root/(engine+'.wav')),
                    '--options-file', str(file), '--output', 'json'])
                self.assertEqual(code, 0, result)
                jid = result['result']['job_id']
                receipt = self.finish(jid)
                self.assertEqual(receipt['state'], 'succeeded')
                self.assertEqual(receipt['audio']['engine'], engine)
                self.assertEqual(receipt['audio']['model_version'], version)
                call = self.calls[-1]
                self.assertEqual(call['engine'], pipeline_engine)
                if version:
                    self.assertEqual(call['engine_options']['model_version'], version)
                    self.assertEqual(call['engine_options']['generation_options'], {'top_k': 40})
                else:
                    self.assertEqual(call['engine_options']['seed'], 7)
                self.assertIn('tts.progress.50', [e.event_type for e in self.control.wait(jid)])

    def test_design_and_official_modes_do_not_require_saved_voice(self):
        self.release.set()
        for engine, mode in [('local_voxcpm2', 'design'), ('local_qwen3_tts', 'design'), ('local_qwen3_tts', 'official')]:
            with self.subTest(engine=engine, mode=mode):
                file = self.root / 'options.json'
                file.write_text(json.dumps({'mode': mode, 'instruction': '温暖自然的中文女声'}))
                code, result = self.invoke(['tts', 'synthesize', '--engine', engine, '--text-file', str(self.text),
                    '--destination', str(self.root/(engine+mode+'.wav')), '--options-file', str(file), '--output', 'json'])
                self.assertEqual(code, 0, result)
                self.assertEqual(self.finish(result['result']['job_id'])['audio']['mode'], mode)
                self.assertIsNone(self.calls[-1]['voice_profile'])

    def test_each_model_cancel_has_no_audio(self):
        from src.core.synthesis.cli_options import MODELS
        for engine in MODELS:
            with self.subTest(engine=engine):
                self.release.clear()
                self.entered.clear()
                code, result = self.invoke(['tts', 'synthesize', '--engine', engine, '--text-file', str(self.text),
                    '--voice-id', self.voice.id, '--destination', str(self.root/(engine+'-cancel.wav')), '--output', 'json'])
                self.assertEqual(code, 0, result)
                self.assertTrue(self.entered.wait(2))
                jid = result['result']['job_id']
                self.runtime.jobs_cancel(jid)
                self.assertEqual(self.finish(jid)['state'], 'cancelled')
                self.assertFalse((self.root/(engine+'-cancel.wav')).exists())

    def test_per_mode_readiness_and_invalid_parameters_never_start_worker(self):
        self.service._readiness_override = lambda engine, opts: 'not_ready' if opts.get('mode') == 'design' else 'ready'
        _, status = self.invoke(['tts', 'status', '--engine', 'local_qwen3_tts', '--output', 'json'])
        qwen = next(row for row in status['result']['models'] if row['engine'] == 'local_qwen3_tts')
        self.assertEqual(qwen['mode_readiness']['design'], 'not_ready')
        self.assertEqual(qwen['mode_readiness']['official'], 'ready')
        file = self.root / 'options.json'
        cases = [('local_qwen3_tts', {'mode': 'design', 'instruction': 'warm'}, 'RUNTIME_NOT_READY'),
                 ('local_indextts2_0', {'do_sample': True}, 'INVALID_ARGUMENT'),
                 ('local_indextts2', {'model_version': '2.0'}, 'INVALID_ARGUMENT'),
                 ('local_qwen3_tts', {'unsupported': True}, 'INVALID_ARGUMENT'),
                 ('local_voxcpm2', {'seed': -1}, 'INVALID_ARGUMENT')]
        for engine, options, error in cases:
            file.write_text(json.dumps(options))
            code, result = self.invoke(['tts', 'synthesize', '--engine', engine, '--text-file', str(self.text),
                '--voice-id', self.voice.id, '--destination', str(self.root/'no.wav'), '--options-file', str(file), '--output', 'json'])
            self.assertNotEqual(code, 0)
            self.assertEqual(result['error']['code'], error, result)
        self.assertEqual(len(self.control.list()), 0)

    def test_installed_control_help_matches_actual_capabilities(self):
        _, payload = self.invoke(['help', '--output', 'json'])
        self.assertEqual(payload['result']['formal_cli_state'], 'available')
        self.assertEqual(payload['result']['business_dispatch'], 'available')
        self.assertTrue(any('tts synthesize' in command for command in payload['result']['business_commands']))
        _, payload = self.invoke(['capabilities', '--output', 'json'])
        tts = next(row for row in payload['result']['capabilities'] if row['capability_id']=='TTS-02')
        self.assertEqual(len(tts['engines']), 4)
        for command in ('status', 'doctor'):
            _, payload = self.invoke([command, '--output', 'json'])
            self.assertEqual(payload['result']['formal_cli_state'], 'available')
        self.assertEqual(payload['result']['network_checks']['attempt_count'], 0)
