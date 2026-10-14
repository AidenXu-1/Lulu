"""Internal native implementation, called only behind the production writer gate.

The disposable frozen probe uses this same implementation. It has no switch to
enable the production entry point and never supplies a hand-written partition.
"""
from contextlib import contextmanager
import ctypes as C
from pathlib import Path
import re
import subprocess
import sys

from ..license.store import SecureStoreError
from .core_task_access import owner_access, verify_core_access, verify_core_request_access


class _CoreItemMissing(Exception):
    pass


def validate_keychain_location(path, *, home=None):
    home = Path.home() if home is None else home
    directory = home / 'Library' / 'Keychains'
    path = Path(path)
    # Resolve both sides: no substring matching or symlink aliases.
    if (not path.is_absolute() or path.parent != directory or
        not path.name.endswith('.keychain-db') or path.resolve() != path or
        directory.resolve() != directory):
        raise SecureStoreError('JOB_CORE_KEYCHAIN_FORMAT_REQUIRED')
    return path


def require_integrity_enabled(backend):
    f = backend.api._found
    f.CFPreferencesCopyValue.restype = C.c_void_p
    f.CFPreferencesCopyValue.argtypes = (C.c_void_p,) * 4
    f.CFGetTypeID.restype = C.c_ulong
    f.CFGetTypeID.argtypes = (C.c_void_p,)
    f.CFBooleanGetTypeID.restype = C.c_ulong
    f.CFBooleanGetTypeID.argtypes = ()
    f.CFBooleanGetValue.restype = C.c_bool
    f.CFBooleanGetValue.argtypes = (C.c_void_p,)
    key, app = backend._string('KeychainIntegrity'), backend._string('com.apple.security')
    value = None
    try:
        value = f.CFPreferencesCopyValue(key, app,
            C.c_void_p.in_dll(f, 'kCFPreferencesAnyUser'),
            C.c_void_p.in_dll(f, 'kCFPreferencesCurrentHost'))
        if value and (f.CFGetTypeID(value) != f.CFBooleanGetTypeID() or not f.CFBooleanGetValue(value)):
            raise SecureStoreError('JOB_CORE_KEYCHAIN_FORMAT_REQUIRED')
    finally:
        for ref in (value, app, key): backend._release(ref)


def require_keychain_preconditions(backend, keychain):
    sec = backend.api._sec
    sec.SecKeychainGetPath.restype = C.c_int32
    sec.SecKeychainGetPath.argtypes = (C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p)
    size, buffer = C.c_uint32(4096), C.create_string_buffer(4096)
    backend.api.Error.raise_for_status(sec.SecKeychainGetPath(keychain, C.byref(size), buffer))
    path = validate_keychain_location(Path(buffer.value.decode('utf-8')))
    require_integrity_enabled(backend)
    return str(path)


def measured_core(core):
    core = Path(core)
    if (not getattr(sys, 'frozen', False) or Path(sys.executable) != core or
        core.is_symlink() or not core.is_file()):
        raise SecureStoreError('JOB_CORE_IDENTITY_REQUIRED')
    verify = subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(core)],
        capture_output=True, timeout=10, check=False)
    display = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(core)],
        capture_output=True, text=True, timeout=10, check=False)
    match = re.search(r'^CDHash=([a-f0-9]{40})$', display.stderr, re.M)
    if verify.returncode or display.returncode or match is None or 'Signature=adhoc' not in display.stderr:
        raise SecureStoreError('JOB_CORE_SIGNATURE_REQUIRED')
    return match[1]


@contextmanager
def core_request_access(backend, core):
    refs = []
    def keep(ref): refs.append(ref); return ref
    try:
        trusted, raw = backend._trusted_application_from_path(core)
        keep(trusted)
        access = keep(owner_access(backend))
        apps = keep(backend._array([trusted]))
        label = keep(backend._string('Lulu Core task storage'))
        acl = C.c_void_p()
        backend.api.Error.raise_for_status(backend.api._sec.SecACLCreateWithSimpleContents(
            access, apps, label, 0, C.byref(acl)))
        keep(acl)
        authorizations = keep(backend._array([backend.api.k_('kSecACLAuthorizationAny')]))
        backend.api.Error.raise_for_status(backend.api._sec.SecACLUpdateAuthorizations(acl, authorizations))
        verify_core_request_access(backend, access, raw)
        yield access, raw
    finally:
        for ref in reversed(refs): backend._release(ref)


class NativeCoreTaskStore:
    """Already-gated caller; pins all operations to one keychain and identity."""
    allowed_identities = None
    def __init__(self, backend, service, core):
        self.backend, self.service, self.core = backend, service, Path(core)
        self.cdhash = measured_core(self.core)

    @contextmanager
    def _keychain(self):
        keychain = self.backend._keychain_ref()
        try:
            require_keychain_preconditions(self.backend, keychain)
            yield keychain
        finally:
            self.backend._release(keychain)

    @contextmanager
    def _query(self, keychain, name, **extra):
        b = self.backend
        search = b._search_list(keychain)
        query = None
        try:
            query = b.api.create_query(kSecClass=b.api.k_('kSecClassGenericPassword'),
                kSecMatchSearchList=search, kSecAttrService=self.service,
                kSecAttrAccount=name, **extra)
            yield query
        finally:
            for ref in (query, search): b._release(ref)

    def _verify_item(self, item, *, creation=False):
        b = self.backend
        if not item.value:
            raise SecureStoreError('JOB_CORE_ITEM_REFERENCE_REQUIRED')
        access, trusted = C.c_void_p(), None
        try:
            b.api.Error.raise_for_status(b.api._sec.SecKeychainItemCopyAccess(item, C.byref(access)))
            trusted, raw = b._trusted_application_from_path(self.core)
            if creation or self.allowed_identities is None:
                verified = verify_core_access(b, access, self.cdhash, raw)
            else:
                from .core_task_access import verify_authenticated_core_access
                verified = verify_authenticated_core_access(b, access, self.cdhash, raw, self.allowed_identities)
            self.last_verified_access = verified
            return verified
        finally:
            for ref in (trusted, access): b._release(ref)

    @contextmanager
    def _item(self, keychain, name):
        b, item = self.backend, C.c_void_p()
        try:
            with self._query(keychain, name, kSecReturnRef=True, kSecMatchLimit=b.api.k_('kSecMatchLimitOne')) as query:
                status = b.api.SecItemCopyMatching(query, C.byref(item))
                if status == -25300:
                    raise _CoreItemMissing()
                b.api.Error.raise_for_status(status)
            self._verify_item(item)
            yield item
        finally:
            b._release(item)

    def create(self, name, value):
        b, item, query, secret = self.backend, C.c_void_p(), None, None
        with self._keychain() as keychain, core_request_access(b, self.core) as (access, _):
            created = False
            try:
                secret = b._secret_data(value)
                query = b.api.create_query(kSecClass=b.api.k_('kSecClassGenericPassword'),
                    kSecUseKeychain=keychain, kSecAttrService=self.service,
                    kSecAttrAccount=name, kSecAttrAccess=access,
                    kSecValueData=secret, kSecReturnRef=True)
                b.api.Error.raise_for_status(b.api.SecItemAdd(query, C.byref(item)))
                created = True
                return self._verify_item(item, creation=True)
            except Exception as exc:
                # Keep the interrupted creation for the Core intent protocol.
                # A duplicate is never deleted, rewritten or silently retried.
                if created:
                    raise SecureStoreError('JOB_CORE_MATERIAL_CREATED_UNVERIFIED') from exc
                raise
            finally:
                for ref in (item, query, secret): b._release(ref)

    def read(self, name):
        try:
            return self._read_existing(name)
        except _CoreItemMissing:
            return None

    def _read_existing(self, name):
        b, data = self.backend, C.c_void_p()
        with self._keychain() as keychain, self._item(keychain, name) as item:
            query = match = search = None
            try:
                match = b._array([item])
                search = b._search_list(keychain)
                query = b.api.create_query(kSecMatchItemList=match, kSecReturnData=True,
                    kSecMatchSearchList=search,
                    kSecClass=b.api.k_('kSecClassGenericPassword'),
                    kSecMatchLimit=b.api.k_('kSecMatchLimitOne'))
                b.api.Error.raise_for_status(b.api.SecItemCopyMatching(query, C.byref(data)))
                if not data.value:
                    raise SecureStoreError('JOB_CORE_MATERIAL_READ_FAILED')
                # A user-approved Keychain read may persist a new application
                # identity. Verify the resulting ACL before consuming its value.
                self._verify_item(item)
                return b.api.cfstr_to_str(data)
            finally:
                b._release(data)
                b._release(query)
                b._release(match)
                b._release(search)

    def replace(self, name, expected, value):
        b = self.backend
        if self.read(name) != expected:
            raise SecureStoreError('JOB_CORE_MATERIAL_CHANGED')
        with self._keychain() as keychain, self._item(keychain, name) as item:
            query = update = secret = match = search = None
            try:
                match = b._array([item])
                search = b._search_list(keychain)
                query = b.api.create_query(kSecMatchItemList=match,
                    kSecMatchSearchList=search,
                    kSecClass=b.api.k_('kSecClassGenericPassword'))
                secret = b._secret_data(value)
                update = b.api.create_query(kSecValueData=secret)
                b.api.Error.raise_for_status(b.api._sec.SecItemUpdate(query, update))
                return self._verify_item(item)
            finally:
                for ref in (update, secret, query, match, search): b._release(ref)
