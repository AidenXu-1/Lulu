"""Pure parameter/layout regression; no Security framework or Keychain access."""
import ctypes
import plistlib
import unittest
from src.core.cli_runtime import core_task_access as a


class CoreTaskAccessTests(unittest.TestCase):
    def test_partition_is_an_exact_plist_not_a_trusted_application_path(self):
        self.assertEqual(plistlib.loads(a.partition_payload('a' * 40)),
                         {'Partitions': ['cdhash:' + 'a' * 40]})

    def test_invalid_signature_cannot_reach_a_native_constructor(self):
        for value in ('', '../core', 'a' * 39, 'a' * 41, 'A' * 40, None, 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, 'JOB_CORE_SIGNATURE_REQUIRED'):
                    a.partition_payload(value)

    def test_lp64_layout_matches_the_compiled_macos_sdk_probe(self):
        self.assertEqual([ctypes.sizeof(x) for x in
            (a.Data, a.List, a.Element, a.Owner, a.Prototype, a.Entry, a.ProcessSelector)],
            [16, 24, 40, 32, 152, 160, 12])
        self.assertEqual(a.Prototype.tag.offset, 80)
        self.assertEqual((a.PARTITION_SUBJECT, a.PARTITION_AUTH), (65548, 65538))

    def test_same_payload_with_wrong_subject_is_rejected_before_cf_checks(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        backend = SimpleNamespace(api=SimpleNamespace(_sec=object(), _found=object()))
        rows = [{'subject':123,'tag':b'','authorization':1},
                {'subject':123,'tag':a.PARTITION_TAG,'authorization':a.PARTITION_AUTH,
                 'payload':{'Partitions':['cdhash:'+'a'*40]}}]
        with patch.object(a, '_native_structure', return_value=((257,os.geteuid(),os.getegid(),1),rows)):
            with self.assertRaisesRegex(Exception, 'JOB_CORE_PARTITION_INVALID'):
                a.verify_core_access(backend, object(), 'a'*40, b'core')

    def test_previous_build_identity_is_not_silently_accepted_after_upgrade(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        backend = SimpleNamespace(api=SimpleNamespace(_sec=object(), _found=object()))
        rows = [
            {'subject':123,'tag':b'','authorization':1},
            {'subject':a.PARTITION_SUBJECT,'tag':a.PARTITION_TAG,'authorization':a.PARTITION_AUTH,
             'payload':{'Partitions':['cdhash:'+'a'*40]}},
            {'subject':a.PARTITION_SUBJECT,'tag':a.INTEGRITY_TAG,'authorization':a.INTEGRITY_AUTH,
             'digest':'c'*64},
        ]
        with patch.object(a, '_native_structure', return_value=((257,os.geteuid(),os.getegid(),1),rows)):
            with self.assertRaisesRegex(Exception, 'JOB_CORE_PARTITION_INVALID'):
                a.verify_core_access(backend, object(), 'b'*40, b'same-canonical-path')
