"""A/B rollback/retry through actual native read/update paths and a fake OS.

No real Keychain or credentials. Keep both native ACL views consistent, as in
the captured permission shape, and mutate only the model's stored value.
"""
import ctypes as C
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

import test_historical_acl as fixtures
from test_historical_acl import OLD, NEW, UNKNOWN, CORE
from src.core.cli_runtime.core_task_native import NativeCoreTaskStore
from src.core.license.store import SecureStoreError


class RollbackAccessTests(unittest.TestCase):
    def exercise(self, original, partitions, applications, *, creation=False, allowed=True):
        fixture = fixtures.HistoricalAclTests()
        def operation(backend):
            def ref(out, number): C.cast(out, C.POINTER(C.c_void_p))[0] = number
            state = {'value': 'before', 'queries': []}
            def query(value, out):
                state['queries'].append(value)
                ref(out, 900 if value.get('kSecReturnData') else 700)
                return 0
            def update(value, changes):
                state['queries'].append(value)
                state['value'] = changes['kSecValueData']
                return 0
            def copy(item, out): ref(out, 800); return 0
            def add(value, out): ref(out, 700); return 0
            backend.api.create_query = lambda **kw: kw
            backend.api.k_ = lambda value: value
            backend.api.SecItemCopyMatching = query
            backend.api.SecItemAdd = add
            backend.api._sec.SecKeychainItemCopyAccess = copy
            backend.api._sec.SecItemUpdate = update
            backend.api.cfstr_to_str = lambda _: state['value']
            backend._array = lambda values: tuple(values)
            backend._search_list = lambda _: (1,)
            backend._secret_data = lambda value: value
            backend._trusted_application_from_path = lambda _: (C.c_void_p(2), CORE)
            store = NativeCoreTaskStore.__new__(NativeCoreTaskStore)
            store.backend, store.cdhash, store.core = backend, original, Path(CORE[:-1].decode())
            store.service = 'Lulu.CoreTasks.'+original
            store.allowed_identities = frozenset({OLD, NEW}) if allowed else None
            @contextmanager
            def keychain(): yield C.c_void_p(1)
            store._keychain = keychain
            @contextmanager
            def request(*args): yield C.c_void_p(99), CORE
            if creation:
                with patch('src.core.cli_runtime.core_task_native.core_request_access', request):
                    return store.create('lulu_core_job_anchor_v1:scope', 'new')
            self.assertEqual(store.read('lulu_core_job_anchor_v1:scope'), 'before')
            store.replace('lulu_core_job_anchor_v1:scope', 'before', 'after')
            self.assertEqual(store.read('lulu_core_job_anchor_v1:scope'), 'after')
            self.assertTrue(all(q.get('kSecAttrService', store.service) == store.service
                                for q in state['queries']))
        return fixture.verify(partitions=partitions, applications=applications,
                               operation=operation, original=original)

    def test_authorized_successor_then_rollback_and_candidate_retry_keep_own_writes_usable(self):
        # B read A during preparation; A returns, then reads B before B retries.
        for original, partitions, apps in (
                (OLD, (OLD, NEW, NEW), (NEW, OLD)),
                (NEW, (NEW, OLD, OLD), (OLD, NEW))):
            with self.subTest(original=original):
                with self.assertRaises(SecureStoreError):
                    self.exercise(original, partitions, apps, allowed=False)
                self.exercise(original, partitions, apps)

    def test_new_material_stays_single_identity_even_when_other_code_is_known(self):
        for original, other in ((OLD, NEW), (NEW, OLD)):
            with self.subTest(original=original):
                self.exercise(original, (original,), (original,), creation=True)
                with self.assertRaisesRegex(SecureStoreError, 'JOB_CORE_MATERIAL_CREATED_UNVERIFIED'):
                    self.exercise(original, (original, other), (other, original), creation=True)

    def test_observed_application_only_successor_keeps_current_generation_writable(self):
        # Captured .3 anchor: ordinary ACL remembers .2, partition retains .3.
        for original, other in ((OLD, NEW), (NEW, OLD)):
            self.exercise(original, (original,), (other, original))
            with self.assertRaises(SecureStoreError):
                self.exercise(original, (original,), (other, original), allowed=False)
            with self.assertRaisesRegex(SecureStoreError, 'JOB_CORE_MATERIAL_CREATED_UNVERIFIED'):
                self.exercise(original, (original,), (other, original), creation=True)

    def test_unknown_or_missing_original_still_stops_existing_writes(self):
        for partitions, apps in (((OLD, UNKNOWN), (UNKNOWN, OLD)),
                                 ((NEW,), (NEW,)), ((OLD, NEW), (NEW,))):
            with self.subTest(partitions=partitions, apps=apps), self.assertRaises(SecureStoreError):
                self.exercise(OLD, partitions, apps)
