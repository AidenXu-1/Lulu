"""Storage protocol failures through a fake OS boundary; no Keychain access."""
import unittest
import ctypes as C
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from src.core.cli_runtime import core_task_access as access
from src.core.license.store import SecureStoreError


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
