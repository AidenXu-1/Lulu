"""Captured ACL shape at fake native boundaries; no system Keychain access."""
import ctypes as C
import os
import plistlib
import struct
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.core.cli_runtime import core_task_access as a, historical_access as h
from src.core.license.store import SecureStoreError

OLD, NEW, UNKNOWN = 'a'*40, 'b'*40, 'c'*40
CORE = b'/Applications/Lulu.app/Contents/MacOS/lulu-agent-core\0'


class HistoricalAclTests(unittest.TestCase):
    def verify(self, *, partitions=(OLD, NEW), applications=(NEW, OLD), paths=None,
               owner=None, prompt=0, stored_extra=None, strict=False, system_partitions=None,
               operation=None, original=OLD):
        paths = list(paths or [CORE]*len(applications))
        payload = {'Partitions': ['cdhash:'+v for v in partitions]}
        rows = [{'subject':123,'tag':b'','authorization':1},
            {'subject':a.PARTITION_SUBJECT,'tag':a.PARTITION_TAG,'authorization':a.PARTITION_AUTH,'payload':payload},
            {'subject':a.PARTITION_SUBJECT,'tag':a.INTEGRITY_TAG,'authorization':a.INTEGRITY_AUTH,'digest':'d'*64}]
        if not strict:
            rows[0]['code_subjects'] = [{'path':p,'cdhash':v} for p,v in zip(paths,applications)]
        if stored_extra is not None: rows.append(stored_extra)
        arrays = {1:[10,11,12,13],20:['ACLAuthorizationChangeACL'],21:['ACLAuthorizationIntegrity'],
                  22:['ACLAuthorizationPartitionID'],23:['ACLAuthorizationAny'],30:list(range(100,100+len(paths)))}
        def ptr(value): return value.value if isinstance(value,C.c_void_p) else value
        def put(out,value): C.cast(out,C.POINTER(C.c_void_p))[0]=value
        def copy(_access,out): put(out,1);return 0
        def contents(acl,apps,label,flags):
            if acl==11:put(label,41)
            elif acl==12:put(label,42)
            elif acl==13:put(apps,30);C.cast(flags,C.POINTER(C.c_uint32))[0]=prompt
            return 0
        sec=SimpleNamespace(SecAccessCopyACLList=copy,SecACLCopyAuthorizations=lambda acl:acl+10,
                            SecACLCopyContents=contents)
        found=SimpleNamespace(CFArrayGetCount=lambda ref:len(arrays[ptr(ref)]),
            CFArrayGetValueAtIndex=lambda ref,j:arrays[ptr(ref)][j])
        system_payload = payload if system_partitions is None else {
            'Partitions':['cdhash:'+v for v in system_partitions]}
        labels={41:'d'*64,42:plistlib.dumps(system_payload).hex()}
        backend=SimpleNamespace(api=SimpleNamespace(_sec=sec,_found=found,
            Error=SimpleNamespace(raise_for_status=lambda status:self.assertEqual(status,0))),
            _cfstring_text=lambda ref:labels[ptr(ref)] if ptr(ref) in labels else ref,
            _copy_trusted_application_data=lambda ref:paths[ref-100],_release=Mock())
        def native(_backend, _access, *, include_trusted=False):
            returned = rows if include_trusted else [
                {k:v for k,v in row.items() if k != 'code_subjects'} for row in rows]
            return owner or (257,os.geteuid(),os.getegid(),1), returned
        with patch.object(a,'_native_structure',side_effect=native):
            if operation is not None:return operation(backend)
            if strict:return a.verify_core_access(backend,object(),original,CORE)
            return a.verify_historical_core_access(backend,object(),original,CORE,frozenset({OLD,NEW}))

    def test_observed_remembered_successor_shape_is_readable_only_as_history(self):
        result=self.verify()
        self.assertEqual(result['partition'],{'Partitions':['cdhash:'+OLD,'cdhash:'+NEW]})
        with self.assertRaises(SecureStoreError):self.verify(strict=True)

    def test_allow_once_and_partition_only_remember_keep_original_identity(self):
        self.verify(partitions=(OLD,),applications=(OLD,))
        self.verify(partitions=(OLD,NEW),applications=(OLD,))

    def test_repeated_known_identities_keep_the_same_read_only_authority(self):
        for partitions, applications in [((OLD, OLD), (OLD,)),
                ((OLD, NEW, NEW), (NEW, OLD)),
                ((OLD, NEW), (NEW, NEW, OLD)),
                ((OLD, OLD, NEW, NEW), (OLD, OLD, NEW, NEW))]:
            with self.subTest(partitions=partitions, applications=applications):
                self.verify(partitions=partitions, applications=applications)
                with self.assertRaises(SecureStoreError):
                    self.verify(partitions=partitions, applications=applications, strict=True)

    def test_application_and_partition_identities_are_independently_authenticated(self):
        self.verify(partitions=(OLD,), applications=(NEW, OLD))
        self.verify(partitions=(OLD,), applications=(NEW, NEW, OLD))

    def test_unknown_and_missing_identities_fail(self):
        for p,apps in [((OLD,UNKNOWN),(OLD,UNKNOWN)),((NEW,),(NEW,)),
                       ((OLD,NEW),(NEW,)),((OLD,),(OLD,UNKNOWN)),
                       ((OLD,UNKNOWN,UNKNOWN),(OLD,)),
                       ((OLD,NEW),(OLD,UNKNOWN,UNKNOWN))]:
            with self.subTest(partitions=p,applications=apps),self.assertRaises(SecureStoreError):
                self.verify(partitions=p,applications=apps)

    def test_repeated_entries_remain_bounded_and_keep_original_structure_checks(self):
        self.verify(partitions=(OLD,)*1024, applications=(OLD,)*1024)
        for options in ({'partitions':(OLD,)*1025},
                        {'applications':(OLD,)*1025},
                        {'partitions':(NEW,NEW), 'applications':(NEW,NEW)},
                        {'applications':(OLD,OLD), 'paths':[CORE,b'/usr/bin/security\0']}):
            with self.subTest(options=options),self.assertRaises(SecureStoreError):
                self.verify(**options)

    def test_second_native_view_must_match_raw_multiplicity(self):
        with self.assertRaises(SecureStoreError):
            self.verify(partitions=(OLD,NEW,NEW), system_partitions=(OLD,NEW))

    def test_path_owner_prompt_and_extra_acl_remain_mandatory(self):
        for options in ({'paths':[b'/usr/bin/security\0',CORE]},
                        {'owner':(257,os.geteuid()+1,os.getegid(),1)},
                        {'prompt':1},{'stored_extra':{'subject':1,'authorization':1,'tag':b''}}):
            with self.subTest(options=options),self.assertRaises(SecureStoreError):self.verify(**options)


class SubjectFixture:
    def __init__(self):self.keep=[]
    def word(self,value):return ('word',value)
    def data(self,value):return ('data',value)
    def sub(self,value):return ('sub',value)
    def linked(self,values):
        array=(a.Element*len(values))();self.keep.append(array)
        for i,(kind,value) in enumerate(values):
            if kind=='word':array[i].kind=2;array[i].word_id=value
            elif kind=='data':
                buf=C.create_string_buffer(value);self.keep.append(buf)
                array[i].value.word=a.Data(len(value),C.addressof(buf))
            else:array[i].kind=1;array[i].value.sublist=value
            if i+1<len(values):array[i].next=C.pointer(array[i+1])
        return a.List(0,C.pointer(array[0]),C.pointer(array[-1]))
    def app(self,path=CORE,requirement=b'requirement',*,extra=False,subject=116):
        values=[self.word(subject),self.word(1),self.data(b'k'*20),self.data(path)]
        if requirement is not None:values.append(self.data(requirement))
        if extra:values.append(self.data(b'unknown auxiliary'))
        return self.linked(values)
    def subject(self,apps=None,threshold=1):
        apps=apps or [self.app()]
        prompt=self.linked([self.word(65536),self.data(struct.pack('=HH',257,0)),self.data(b'Lulu')])
        return self.linked([self.word(123),self.word(threshold),self.word(len(apps)+1),
                            *[self.sub(app) for app in apps],self.sub(prompt)])


class SubjectDecoderTests(unittest.TestCase):
    def test_real_cssm_tree_accepts_bounded_repeated_requirements(self):
        for count in (2,1024,1025):
            fixture=SubjectFixture();subject=fixture.subject([fixture.app() for _ in range(count)])
            with self.subTest(count=count),patch.object(h,'requirement_cdhash',return_value=OLD):
                if count > 1024:
                    with self.assertRaises(SecureStoreError):h.code_subjects(object(),subject)
                else:
                    self.assertEqual(h.code_subjects(object(),subject),[{'path':CORE,'cdhash':OLD}]*count)

    def test_public_cssm_subject_preserves_code_identity_separate_from_path(self):
        fixture=SubjectFixture();subject=fixture.subject([fixture.app(requirement=b'old'),fixture.app(requirement=b'new')])
        with patch.object(h,'requirement_cdhash',side_effect=lambda b,raw:OLD if raw==b'old' else NEW):
            self.assertEqual(h.code_subjects(object(),subject),[{'path':CORE,'cdhash':OLD},{'path':CORE,'cdhash':NEW}])

    def test_unknown_subject_missing_requirement_threshold_tail_and_cycles_fail(self):
        for case in ('any','missing','extra','threshold','tail','cycle','count'):
            fixture=SubjectFixture()
            app=fixture.app(subject=1 if case=='any' else 116,requirement=None if case=='missing' else b'requirement',extra=case=='extra')
            subject=fixture.subject([app],threshold=2 if case=='threshold' else 1)
            if case=='tail':subject.tail=subject.head
            if case=='cycle':subject.tail.contents.next=subject.head
            if case=='count':subject.head.contents.next.contents.next.contents.word_id=30
            with self.subTest(case=case),patch.object(h,'requirement_cdhash',return_value=OLD),self.assertRaises(SecureStoreError):
                h.code_subjects(object(),subject)

    def test_requirement_decoder_rejects_broad_expressions_and_releases_refs(self):
        def create(data,flags,out):C.cast(out,C.POINTER(C.c_void_p))[0]=2;return 0
        def copy(req,flags,out):C.cast(out,C.POINTER(C.c_void_p))[0]=3;return 0
        backend=SimpleNamespace(api=SimpleNamespace(_sec=SimpleNamespace(
            SecRequirementCreateWithData=create,SecRequirementCopyString=copy)),
            _bytes_data=lambda raw:C.c_void_p(1),_release=Mock(),_cfstring_text=Mock())
        for text in (f'cdhash H"{OLD}" or true','identifier "Lulu"','always',f'cdhash H"{OLD[:-1]}"'):
            backend._cfstring_text.return_value=text;backend._release.reset_mock()
            with self.subTest(text=text),self.assertRaises(SecureStoreError):h.requirement_cdhash(backend,b'fixture')
            self.assertEqual([call.args[0].value for call in backend._release.call_args_list],[3,2,1])
        backend._cfstring_text.return_value=f'cdhash H"{OLD}"'
        self.assertEqual(h.requirement_cdhash(backend,b'fixture'),OLD)


class ReadBoundaryTests(unittest.TestCase):
    def test_failure_identifies_only_closed_storage_role(self):
        from src.core.agent_entry.standalone_history import historical_keys
        from src.core.cli_runtime.core_task_native import NativeCoreTaskStore
        keys=SimpleNamespace(service='Lulu',_keyring=lambda:object())
        with patch('src.core.agent_entry.standalone_paths.installed_app'), \
             patch('src.core.cli_runtime.core_task_keychain.current_core',return_value=Path('/fixture/core')), \
             patch('src.core.cli_runtime.core_task_keychain._assert_core_account'), \
             patch('src.core.cli_runtime.core_task_native.measured_core',return_value=NEW), \
             patch.object(NativeCoreTaskStore,'read',side_effect=lambda *a:(_ for _ in ()).throw(SecureStoreError('JOB_CORE_ACCESS_INVALID'))):
            reader=historical_keys(keys,Path(os.fsdecode(CORE[:-1])),OLD)
            for name,role in [('lulu_core_job_registry_v1','core_registry'),
                    ('lulu_core_job_key_v1:private-account','core_key'),
                    ('lulu_core_job_anchor_v1:private-account','core_anchor')]:
                with self.subTest(role=role),self.assertRaises(SecureStoreError) as failure:
                    reader.get_text(name)
                self.assertEqual(failure.exception.lulu_storage_item_kind,role)

    def test_post_read_accepts_known_repetition_but_rejects_unknown_before_conversion(self):
        from test_core_task_native import PrivateKeychainQueryTests
        for after in ((OLD,NEW,NEW),(OLD,UNKNOWN,UNKNOWN)):
            store,state=PrivateKeychainQueryTests().private_store()
            views=iter(((OLD,NEW),after))
            store._verify_item=lambda _item: HistoricalAclTests().verify(partitions=next(views))
            store.backend.api.cfstr_to_str=Mock(return_value='fixture-only')
            with self.subTest(after=after):
                if UNKNOWN in after:
                    with self.assertRaises(SecureStoreError):store.read('key')
                    store.backend.api.cfstr_to_str.assert_not_called()
                else:
                    self.assertEqual(store.read('key'),'fixture-only')
                    store.backend.api.cfstr_to_str.assert_called_once()

    def test_read_rechecks_same_item_before_secret_conversion(self):
        from test_core_task_native import PrivateKeychainQueryTests
        store,state=PrivateKeychainQueryTests().private_store()
        store._verify_item=Mock(side_effect=[{},SecureStoreError('JOB_CORE_ACCESS_INVALID')])
        store.backend.api.cfstr_to_str=Mock(side_effect=AssertionError('must not consume value'))
        with self.assertRaises(SecureStoreError):store.read('key')
        self.assertEqual([call.args[0].value for call in store._verify_item.call_args_list],[700,700])
        store.backend.api.cfstr_to_str.assert_not_called()
        self.assertEqual(len(state['queries']),2)

    def test_historical_reader_cannot_create_or_replace_any_item(self):
        from src.core.agent_entry.standalone_history import historical_keys
        from src.core.cli_runtime.core_task_native import NativeCoreTaskStore
        keys=SimpleNamespace(service='Lulu',_keyring=lambda:object())
        def read(reader,name):
            for method in (reader.create,reader.replace):
                with self.assertRaisesRegex(SecureStoreError,'HISTORICAL_STORAGE_READ_ONLY'):method('name','value')
            return 'memory-only'
        with patch('src.core.agent_entry.standalone_paths.installed_app'), \
             patch('src.core.cli_runtime.core_task_keychain.current_core',return_value=Path('/fixture/core')), \
             patch('src.core.cli_runtime.core_task_keychain._assert_core_account'), \
             patch('src.core.cli_runtime.core_task_native.measured_core',return_value=NEW), \
             patch.object(NativeCoreTaskStore,'read',read):
            reader=historical_keys(keys,Path(os.fsdecode(CORE[:-1])),OLD)
            self.assertEqual(reader.get_text('lulu_core_job_registry_v1'),'memory-only')
