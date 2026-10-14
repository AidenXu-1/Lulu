import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.diagnostics import record_failure


class LocalFailureDiagnosticsTests(unittest.TestCase):
    def test_historical_storage_role_and_closed_reason_do_not_log_values(self):
        from src.core.cli_runtime.historical_access import historical_partition
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for role in ('core_registry','core_key','core_anchor','private-account-secret'):
                try:
                    historical_partition([{'authorization':65538,'payload':{'Partitions':['private-key-value']}}],
                                         'a'*40,frozenset({'a'*40}))
                except RuntimeError as exc:
                    exc.lulu_storage_item_kind=role
                    record_failure('installation_preparation',exc,root=root)
            raw=(root/'diagnostics/failures.json').read_text()
            self.assertNotIn('private-account-secret',raw)
            self.assertNotIn('private-key-value',raw)
            events=json.loads(raw)['events']
            for event,role in zip(events,('core_registry','core_key','core_anchor',None)):
                self.assertEqual(event['causes'][0].get('storage_item_kind'),role)
                self.assertEqual(event['causes'][0]['code'],'JOB_CORE_ACCESS_INVALID')
                self.assertEqual(event['causes'][1]['code'],'JOB_CORE_HISTORICAL_PARTITION_FORMAT_INVALID')

    def test_native_timeout_is_distinguished_from_generic_evidence_error(self):
        from src.core.agent_entry.native_transport import XpcTransportError
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record_failure('gui_task_connect', XpcTransportError(21), root=root)
            cause = json.loads((root/'diagnostics/failures.json').read_text())['events'][0]['causes'][0]
            self.assertEqual(cause['native_code'], 21)

    def test_closed_diagnostics_keep_code_and_location_without_exception_data(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            try:
                try:
                    raise ValueError('secret-token-and-private-transcript')
                except ValueError as cause:
                    raise RuntimeError('GUI_WORKER_CLAIM_CONFLICT') from cause
            except RuntimeError as exc:
                record_failure('gui_task_save',exc,root=root)
            path=root/'diagnostics/failures.json'
            raw=path.read_text()
            self.assertNotIn('secret-token',raw)
            self.assertNotIn('private-transcript',raw)
            event=json.loads(raw)['events'][0]
            self.assertEqual(event['causes'][0]['code'],'GUI_WORKER_CLAIM_CONFLICT')
            self.assertEqual(event['causes'][1]['type'],'ValueError')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
