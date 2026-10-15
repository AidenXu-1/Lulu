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
