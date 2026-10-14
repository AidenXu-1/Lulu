"""Real CLI adapters with isolated requests; no network, credentials or models."""
import io
import json
import shlex
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from src.agent_cli.catalog import capability_discovery
from src.agent_cli.main import main, _parse_link_options
from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
from src.core.transcription_job_application import LocalTranscriptionJobApplication
from src.core.job_ledger.service import JobControlService


class CliAuditTests(unittest.TestCase):
    def runtime(self):
        return LocalTranscriptionCliRuntime(Mock(spec=LocalTranscriptionJobApplication))

    def test_advertised_media_save_command_parses_with_directory(self):
        rows = capability_discovery(activated_capability_ids=frozenset({'TR-07'}))['capabilities']
        example = next(r['argv_template'] for r in rows if r['capability_id']=='TR-07')
        argv = shlex.split(example.replace('{prepare|run}', 'prepare').replace('<url>', 'https://example.com/video').replace('<directory>', '/tmp/media'))[1:]
        _, options = _parse_link_options(argv)
        self.assertIs(options['save_media'], True)
        self.assertEqual(options['media_directory'], '/tmp/media')

    def test_unconnected_url_never_falls_through_to_local_file_execution(self):
        for verb in ('prepare', 'run'):
            runtime = self.runtime()
            getattr(runtime._application, verb).side_effect = AssertionError('URL reached local file handler')
            out = io.StringIO()
            code = main(['transcribe', verb, 'https://example.com/video', '--output', 'json'],
                        stdout=out, local_transcription_runtime=runtime)
            self.assertEqual(code, 30)
            self.assertEqual(json.loads(out.getvalue())['error']['code'], 'CAPABILITY_UNAVAILABLE')
            getattr(runtime._application, verb).assert_not_called()

    def test_history_cancel_and_wait_return_closed_errors(self):
        for verb, extra in [('cancel', []), ('wait', ['--after-sequence', '0'])]:
            runtime = self.runtime()
            runtime._history = {'old-job': {'job_id': 'old-job'}}
            runtime._job_control = Mock(spec=JobControlService)
            out, err = io.StringIO(), io.StringIO()
            code = main(['jobs', verb, '--job-id', 'old-job', *extra, '--output', 'jsonl' if verb=='wait' else 'json'],
                        stdout=out, stderr=err, local_transcription_runtime=runtime)
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())['error']['code'], 'CAPABILITY_UNAVAILABLE')
            self.assertEqual(err.getvalue(), '')
            runtime._job_control.cancel.assert_not_called()

    def test_new_business_adapters_keep_dependency_noise_out_of_json(self):
        def noisy(*args, **kwargs):
            print('private dependency stdout')
            print('private dependency stderr', file=__import__('sys').stderr)
            return {'entries': []}
        commands = [
            (['workspace', 'documents', 'list', '--request', '/fixture.json', '--output', 'json'], 'src.agent_cli.workspace_commands.execute'),
            (['library', 'list', '--output', 'json'], 'src.agent_cli.library_commands.execute'),
            (['tts', 'status', '--output', 'json'], 'src.agent_cli.local_transcription.LocalTranscriptionCliRuntime.tts_status'),
        ]
        for argv, target in commands:
            out, err = io.StringIO(), io.StringIO()
            with patch(target, side_effect=noisy), redirect_stdout(out), redirect_stderr(err):
                code = main(argv, local_transcription_runtime=self.runtime())
            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue(), '')
            self.assertTrue(json.loads(out.getvalue())['ok'])
            self.assertNotIn('private', out.getvalue())
