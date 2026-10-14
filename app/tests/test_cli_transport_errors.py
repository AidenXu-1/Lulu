import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.agent_entry.entrypoints import run_cli_via_core
from src.core.agent_entry.native_transport import XpcTransportError


class CliTransportErrorsTests(unittest.TestCase):
    def invoke(self, native_code, dispatched=False):
        output, errors = io.StringIO(), io.StringIO()
        bridge = MagicMock()
        with patch('src.core.agent_entry.entrypoints.load_peer_requirements', return_value=(None, None, {})), patch('src.core.agent_entry.entrypoints.MeasuredCoreClient') as client:
            client.return_value.business_dispatched = dispatched
            client.return_value.dispatch.side_effect = XpcTransportError(native_code, 'secret text must never appear')
            result = run_cli_via_core(['tts', 'status', '--output', 'json'], resource_root=Path('/Applications/Lulu.app/Contents/Resources'), stdout=output, stderr=errors, bridge=bridge)
            self.assertEqual(client.return_value.dispatch.call_count, 1)
        self.assertEqual(errors.getvalue(), '')
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())
        return result, json.loads(output.getvalue())

    def test_pre_dispatch_failure_is_closed_and_has_zero_effects(self):
        for native_code in [20, 21]:
            with self.subTest(native_code=native_code):
                code, reply = self.invoke(native_code)
                self.assertEqual(code, 30)
                self.assertEqual(reply['schema_id'], 'lulu.agent/v1')
                self.assertFalse(reply['ok'])
                self.assertEqual(reply['command'], 'tts.status')
                self.assertEqual(reply['error']['details']['effect_count'], 0)
                self.assertEqual(reply['error']['details']['native_code'], native_code)
                self.assertFalse(reply['error']['retryable'])

    def test_dispatched_timeout_does_not_claim_zero_or_replay(self):
        code, reply = self.invoke(21, True)
        self.assertEqual(code, 21)
        self.assertEqual(reply['error']['details']['effect_count'], 'unknown')
        self.assertEqual(reply['error']['details']['reason_code'], 'CORE_REQUEST_OUTCOME_UNKNOWN')
        self.assertFalse(reply['error']['retryable'])

    def test_peer_rejection_stays_integrity_error(self):
        code, reply = self.invoke(11)
        self.assertEqual(code, 60)
        self.assertEqual(reply['error']['code'], 'INTEGRITY_ERROR')


if __name__ == '__main__':
    unittest.main()
