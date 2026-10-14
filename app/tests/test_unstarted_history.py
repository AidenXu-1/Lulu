"""Interrupted first storage setup: real files, in-memory keys, no OS access."""
from copy import deepcopy
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_core_storage_binding import CoreMemoryKeys
from test_phase5_job_ledger import _create_arguments
from src.core.agent_entry.standalone_history import verified_terminal_rows
from src.core.agent_entry.product_transaction import snapshot_digest
from src.core.job_ledger.storage_binding import prepare_core_task_store, REGISTRY_KEY


class UnstartedHistoryTests(unittest.TestCase):
    def test_each_unstarted_cut_can_be_read_without_mutation_then_resumed_by_owner(self):
        for cut in ('registered', 'intent', 'key', 'binding', 'ledger'):
            with self.subTest(cut=cut), TemporaryDirectory() as directory:
                root = Path(directory).resolve()/'generation'; root.mkdir(mode=0o700)
                keys = CoreMemoryKeys()
                with self.assertRaisesRegex(RuntimeError, 'CORE_STORAGE_CRASH'):
                    prepare_core_task_store(root, 'installation-001', keys,
                                            crash_after=cut, legacy_storage=False)
                before, values, writes = snapshot_digest(root), deepcopy(keys.values), list(keys.writes)
                self.assertEqual(verified_terminal_rows(root, 'installation-001', keys), [])
                self.assertEqual(snapshot_digest(root), before)
                self.assertEqual((keys.values, keys.writes), (values, writes))
                self.assertEqual(prepare_core_task_store(root, 'installation-001', keys,
                                                       legacy_storage=False).list_jobs(), ())

    def test_intent_never_hides_existing_business_records_or_unknown_content(self):
        for attack in ('business', 'unknown', 'binding', 'missing_key', 'wrong_root', 'anchor', 'pending', 'both'):
            with self.subTest(attack=attack), TemporaryDirectory() as directory:
                root = Path(directory).resolve()/'generation'; root.mkdir(mode=0o700)
                keys = CoreMemoryKeys()
                ledger = prepare_core_task_store(root, 'installation-001', keys, legacy_storage=False)
                if attack == 'business': ledger.create_job(**_create_arguments())
                record = json.loads(keys.values[REGISTRY_KEY]);record['phase'] = 'INTENT';record['binding_sha256'] = None
                if attack == 'wrong_root':record['body']['root'] = str(root.parent/'different')
                keys.values[REGISTRY_KEY] = json.dumps(record)
                if attack == 'unknown':(root/'unknown.bin').write_bytes(b'preserve')
                if attack == 'binding':(root/'task-integrity-v1/binding.json').write_bytes(b'{}')
                if attack == 'missing_key':
                    keys.values.pop(next(k for k in keys.values if k.startswith('lulu_core_job_key_v1:')))
                if attack == 'anchor':
                    name = next(k for k in keys.values if k.startswith('lulu_core_job_anchor_v1:'))
                    head = json.loads(keys.values[name]); head['sequence'] = 1
                    keys.values[name] = json.dumps(head)
                if attack == 'pending':(root/'jobs/job-ledger-v1/journal.pending').write_bytes(b'pending')
                if attack == 'both':shutil.copytree(root/'jobs', root/'task-integrity-v1/prepared-jobs')
                before, values = snapshot_digest(root), deepcopy(keys.values)
                with self.assertRaises(RuntimeError):verified_terminal_rows(root, 'installation-001', keys)
                self.assertEqual(snapshot_digest(root), before)
                self.assertEqual(keys.values, values)
