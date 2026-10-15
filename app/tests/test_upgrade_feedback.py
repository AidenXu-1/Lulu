"""Reported ACL shapes at a fake OS boundary; no real Keychain or credentials."""
import ctypes as C
import io
import os
import plistlib
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.core.cli_runtime import core_task_access as access
from src.core.license.store import SecureStoreError


class FeedbackAclShapeTests(unittest.TestCase):
    def verify(self, *, partitions=None, trusted=None):
        generation = 'a'*40
        expected = b'/Applications/Lulu.app/Contents/MacOS/lulu-agent-core\0'
        partitions = partitions if partitions is not None else ['cdhash:'+generation]
        trusted = trusted if trusted is not None else [expected]
        payload = {'Partitions': partitions}
        rows = [{'subject':123, 'tag':b'', 'authorization':1},
                {'subject':access.PARTITION_SUBJECT, 'tag':access.PARTITION_TAG,
                 'authorization':access.PARTITION_AUTH, 'payload':payload},
                {'subject':access.PARTITION_SUBJECT, 'tag':access.INTEGRITY_TAG,
                 'authorization':access.INTEGRITY_AUTH, 'digest':'c'*64}]
        arrays = {1:[10,11,12,13], 20:['ACLAuthorizationChangeACL'],
                  21:['ACLAuthorizationIntegrity'], 22:['ACLAuthorizationPartitionID'],
                  23:['ACLAuthorizationAny'], 30:list(range(100,100+len(trusted)))}
        def pointer(value): return value.value if isinstance(value,C.c_void_p) else value
        def copy_acls(_ref, out):
            C.cast(out, C.POINTER(C.c_void_p))[0] = 1
            return 0
        def copy_contents(acl, apps, label, prompt):
            if acl == 11: C.cast(label,C.POINTER(C.c_void_p))[0] = 41
            elif acl == 12: C.cast(label,C.POINTER(C.c_void_p))[0] = 42
            elif acl == 13: C.cast(apps,C.POINTER(C.c_void_p))[0] = 30
            return 0
        def status(value):
            if value: raise AssertionError('Unexpected fake OS status')
        sec = SimpleNamespace(SecAccessCopyACLList=copy_acls,
            SecACLCopyAuthorizations=lambda acl:acl+10,
            SecACLCopyContents=copy_contents)
        found = SimpleNamespace(CFArrayGetCount=lambda ref:len(arrays[pointer(ref)]),
            CFArrayGetValueAtIndex=lambda ref,index:arrays[pointer(ref)][index])
        labels = {41:'c'*64, 42:plistlib.dumps(payload).hex()}
        backend = SimpleNamespace(api=SimpleNamespace(_sec=sec,_found=found,
            Error=SimpleNamespace(raise_for_status=status)),
            _cfstring_text=lambda ref:labels[pointer(ref)] if pointer(ref) in labels else ref,
            _copy_trusted_application_data=lambda ref:trusted[ref-100],
            _release=Mock())
        with patch.object(access, '_native_structure',
                return_value=((257,os.geteuid(),os.getegid(),1),rows)):
            return access.verify_core_access(backend, object(), generation, expected)

    def test_normal_old_generation_acl_is_accepted(self):
        result = self.verify()
        self.assertEqual(result['trusted_count'],1)
        self.assertEqual(result['partition'], {'Partitions':['cdhash:'+'a'*40]})

    def test_reported_extra_partition_is_rejected_without_migration(self):
        with self.assertRaisesRegex(SecureStoreError,'JOB_CORE_PARTITION_INVALID'):
            self.verify(partitions=['cdhash:'+'a'*40,'cdhash:'+'b'*40])

    def test_reported_security_tool_and_multiple_apps_remain_rejected(self):
        with self.assertRaisesRegex(SecureStoreError,'JOB_CORE_ACCESS_INVALID'):
            self.verify(trusted=[b'cdhash:'+b'a'*40,b'cdhash:'+b'b'*40,b'/usr/bin/security\0'])


class PreparationFailureOutputTests(unittest.TestCase):
    def test_signed_preparation_emits_closed_code_and_never_exception_text(self):
        from src.core.agent_entry.standalone_runtime import prepare_entry
        for error, expected in (
                (RuntimeError('JOB_CORE_PARTITION_INVALID'),'JOB_CORE_PARTITION_INVALID'),
                (RuntimeError('JOB_PRIVATE_SECRET'),'CORE_OPERATION_FAILED'),
                (RuntimeError('PRIVATE_SECRET:do not expose this'),'CORE_OPERATION_FAILED')):
            with (self.subTest(expected=expected), patch(
                    'src.core.agent_entry.standalone_paths.frozen_app',return_value=Path('/fixture/Lulu.app')),
                    patch('threading.Thread'), patch(
                    'src.core.agent_entry.standalone_runtime.prepare_current_app',side_effect=error), patch(
                    'src.core.agent_entry.standalone_runtime.ensure_service') as service, patch(
                    'src.core.diagnostics.record_failure'), redirect_stdout(io.StringIO()) as output):
                self.assertEqual(prepare_entry([]),1)
                self.assertEqual(output.getvalue(),'LULU_PREPARATION_FAILURE:'+expected+'\n')
                self.assertNotIn('PRIVATE_SECRET',output.getvalue())
                service.assert_not_called()
