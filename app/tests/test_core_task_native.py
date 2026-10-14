"""Storage protocol failures through a fake OS boundary; no Keychain access."""
import unittest
import ctypes as C
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from src.core.cli_runtime import core_task_access as access
from src.core.license.store import SecureStoreError


class CoreRequestTests(unittest.TestCase):
    def test_untrusted_location_is_rejected_before_any_item_write(self):
        from src.core.cli_runtime.core_task_native import validate_keychain_location
        home = Path('/Users/example')
        for path in ('/tmp/synthetic.keychain-db',
                     '/tmp/Library/Keychains/synthetic.keychain-db',
                     '/Users/example/Library/Keychains/synthetic.keychain'):
            with self.subTest(path=path), self.assertRaises(SecureStoreError):
                validate_keychain_location(Path(path), home=home)

    def test_stored_item_without_system_permissions_is_rejected(self):
        from types import SimpleNamespace
        import os
        backend = SimpleNamespace(api=SimpleNamespace(_sec=object(), _found=object()))
        rows = [{'subject':123,'tag':b'','authorization':1}]
        with patch.object(access, '_native_structure', return_value=((257,os.geteuid(),os.getegid(),1),rows)):
            with self.assertRaises(SecureStoreError):
                access.verify_core_access(backend, object(), 'a'*40, b'core')


class NativeCreateTests(unittest.TestCase):
    def make_store(self, add_status=0):
        from src.core.cli_runtime.core_task_native import NativeCoreTaskStore
        calls=[]
        class Error:
            @staticmethod
            def raise_for_status(status):
                if status: raise SecureStoreError(str(status))
        def add(query,out):
            calls.append(('add',query))
            if not add_status: C.cast(out,C.POINTER(C.c_void_p))[0]=700
            return add_status
        def copy(item,out):
            calls.append(('copy_access',item.value))
            C.cast(out,C.POINTER(C.c_void_p))[0]=800
            return 0
        api=SimpleNamespace(Error=Error,k_=lambda s:s,create_query=lambda **kw:kw,
            SecItemAdd=add,_sec=SimpleNamespace(SecKeychainItemCopyAccess=copy))
        b=SimpleNamespace(api=api,_release=lambda ref:None,_secret_data=lambda value:value,
            _trusted_application_from_path=lambda path:(C.c_void_p(900),b'core'))
        store=NativeCoreTaskStore.__new__(NativeCoreTaskStore)
        store.backend,store.core,store.service,store.cdhash=b,Path('/fake/core'),'synthetic','a'*40
        @contextmanager
        def keychain(): yield C.c_void_p(1)
        store._keychain=keychain
        return store,calls

    @contextmanager
    def request(self,*args): yield C.c_void_p(99),b'core'

    def test_creation_verifies_the_actual_returned_item_not_the_request(self):
        store,calls=self.make_store()
        with patch('src.core.cli_runtime.core_task_native.core_request_access',self.request), \
             patch('src.core.cli_runtime.core_task_native.verify_core_access',return_value={'stored':True}) as verify:
            self.assertEqual(store.create('key','synthetic'),{'stored':True})
        self.assertEqual(calls[-1],('copy_access',700))
        self.assertEqual(verify.call_args.args[1].value,800)

    def test_unverified_created_item_is_preserved_without_retry(self):
        store,calls=self.make_store()
        with patch('src.core.cli_runtime.core_task_native.core_request_access',self.request), \
             patch('src.core.cli_runtime.core_task_native.verify_core_access',side_effect=SecureStoreError('wrong partition')):
            with self.assertRaisesRegex(SecureStoreError,'JOB_CORE_MATERIAL_CREATED_UNVERIFIED'):
                store.create('key','synthetic')
        self.assertEqual([c[0] for c in calls],['add','copy_access'])
        self.assertFalse(hasattr(store.backend.api._sec,'SecKeychainItemDelete'))

    def test_duplicate_is_not_read_deleted_or_retried(self):
        store,calls=self.make_store(-25299)
        with patch('src.core.cli_runtime.core_task_native.core_request_access',self.request):
            with self.assertRaisesRegex(SecureStoreError,'-25299'):
                store.create('key','synthetic')
        self.assertEqual([c[0] for c in calls],['add'])

    def test_mismatched_expected_value_cannot_update(self):
        store,calls=self.make_store()
        store.read=lambda name:'interrupted-other-value'
        with self.assertRaisesRegex(SecureStoreError,'JOB_CORE_MATERIAL_CHANGED'):
            store.replace('key','old-value','new-value')
        self.assertEqual(calls,[])


class PrivateKeychainQueryTests(unittest.TestCase):
    """OS model: item-list filters a search, whose default excludes private DB."""
    make_store = NativeCreateTests.make_store
    def private_store(self):
        store,_=self.make_store()
        b=store.backend
        b._array=lambda refs:tuple(ref.value for ref in refs)
        b._search_list=lambda keychain:(keychain.value,)
        state={'value':'before','queries':[]}
        def search(query,out):
            state['queries'].append(query)
            if query.get('kSecMatchSearchList',(2,))!=(1,):return -25300
            if query.get('kSecReturnRef'):
                C.cast(out,C.POINTER(C.c_void_p))[0]=700
            elif query.get('kSecReturnData') and query.get('kSecMatchItemList')==(700,):
                C.cast(out,C.POINTER(C.c_void_p))[0]=900
            else:return -25300
            return 0
        def update(query,changes):
            state['queries'].append(query)
            if query.get('kSecMatchSearchList',(2,))!=(1,) or query.get('kSecMatchItemList')!=(700,):
                return -25300
            state['value']=changes['kSecValueData']
            return 0
        b.api.SecItemCopyMatching=search
        b.api._sec.SecItemUpdate=update
        b.api.cfstr_to_str=lambda pointer:state['value']
        store._verify_item=lambda item:{'verified':item.value==700}
        return store,state

    def test_private_keychain_read_succeeds_without_adding_it_to_default_search(self):
        store,state=self.private_store()
        self.assertEqual(store.read('key'),'before')
        self.assertEqual(len(state['queries']),2)
        self.assertEqual(state['queries'][-1]['kSecMatchItemList'],(700,))

    def test_private_keychain_update_preserves_explicit_db_and_exact_item(self):
        store,state=self.private_store()
        # Isolate update routing from the separately checked value-read route.
        store.read=lambda name:'before'
        self.assertEqual(store.replace('key','before','after'),{'verified':True})
        self.assertEqual(state['value'],'after')
        self.assertEqual(state['queries'][-1]['kSecMatchItemList'],(700,))
