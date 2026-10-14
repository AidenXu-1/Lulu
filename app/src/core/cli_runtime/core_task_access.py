"""Public SDK CSSM representation used only for explicit Core access objects.

No Keychain queries or mutations. All input CSSM storage is Python-owned.
"""
from __future__ import annotations

import ctypes as C
import plistlib
import re
from ..license.store import SecureStoreError

# Security.framework SDK cssmtype.h / cssmapple.h (LP64).
PARTITION_SUBJECT = 0x10000 + 12
PARTITION_AUTH = 0x10000 + 2
PARTITION_TAG = b"___PARTITION___"
INTEGRITY_AUTH = 0x10000 + 3
INTEGRITY_TAG = b"___INTEGRITY___"


class Data(C.Structure):
    _fields_ = [("length", C.c_size_t), ("data", C.c_void_p)]


class Element(C.Structure):
    pass


class List(C.Structure):
    _fields_ = [("kind", C.c_uint32), ("head", C.POINTER(Element)),
                ("tail", C.POINTER(Element))]


class Value(C.Union):
    _fields_ = [("sublist", List), ("word", Data)]


Element._fields_ = [("next", C.POINTER(Element)), ("word_id", C.c_uint32),
                    ("kind", C.c_uint32), ("value", Value)]


class Authorizations(C.Structure):
    _fields_ = [("count", C.c_uint32), ("tags", C.POINTER(C.c_uint32))]


class Validity(C.Structure):
    _fields_ = [("start", Data), ("end", Data)]


class Prototype(C.Structure):
    _fields_ = [("subject", List), ("delegate", C.c_int32),
                ("auth", Authorizations), ("validity", Validity),
                ("tag", C.c_char * 68)]


class Entry(C.Structure):
    _fields_ = [("proto", Prototype), ("handle", C.c_size_t)]


class Owner(C.Structure):
    _fields_ = [("subject", List), ("delegate", C.c_int32)]


def partition_payload(cdhash: str) -> bytes:
    if type(cdhash) is not str or re.fullmatch(r"[a-f0-9]{40}", cdhash) is None:
        raise SecureStoreError("JOB_CORE_SIGNATURE_REQUIRED")
    return plistlib.dumps({"Partitions": ["cdhash:" + cdhash]}, fmt=plistlib.FMT_XML)


def _configure(sec):
    sec.SecAccessCreateFromOwnerAndACL.restype = C.c_int32
    sec.SecAccessCreateFromOwnerAndACL.argtypes = (C.POINTER(Owner), C.c_uint32,
        C.POINTER(Entry), C.POINTER(C.c_void_p))


class ProcessSelector(C.Structure):
    _fields_ = [("version", C.c_uint16), ("mask", C.c_uint16),
                ("uid", C.c_uint32), ("gid", C.c_uint32)]


def owner_access(backend):
    """Create only the UID owner, without ordinary or system ACL entries."""
    import os
    _configure(backend.api._sec)
    selector = ProcessSelector(0x101, 1, os.geteuid(), os.getegid())
    owner_data, owner_head = Element(), Element()
    owner_data.value.word = Data(C.sizeof(selector), C.addressof(selector))
    owner_head.kind, owner_head.word_id = 2, 0x10000 + 3
    owner_head.next = C.pointer(owner_data)
    owner = Owner(List(0, C.pointer(owner_head), C.pointer(owner_data)), 0)
    # SecAccess.cpp requires a non-null pointer even when aclCount is zero.
    # The zeroed storage is not an entry: Access::compile iterates aclCount.
    empty = Entry()
    result = C.c_void_p()
    backend.api.Error.raise_for_status(backend.api._sec.SecAccessCreateFromOwnerAndACL(
        C.byref(owner), 0, C.byref(empty), C.byref(result)))
    if not result.value:
        raise SecureStoreError("JOB_CORE_ACCESS_UNAVAILABLE")
    return result


def _native_structure(backend, access, *, include_trusted=False):
    """Copy, inspect and free the public API's caller-owned CSSM allocation tree.

    Apple Access.cpp copyOwnerAndAcl + ACL.cpp copyAclEntry/copyAclOwner use
    ChunkCopyWalker (walkers.h/cssmlist.h): each node/data buffer is allocated
    separately, Tail aliases the last node. DefaultAllocator uses malloc/free.
    """
    sec = backend.api._sec
    sec.SecAccessGetOwnerAndACL.restype = C.c_int32
    sec.SecAccessGetOwnerAndACL.argtypes = (C.c_void_p, C.POINTER(C.POINTER(Owner)),
        C.POINTER(C.c_uint32), C.POINTER(C.POINTER(Entry)))
    owner, entries, count = C.POINTER(Owner)(), C.POINTER(Entry)(), C.c_uint32()
    owned = set()
    def address(pointer):
        return C.cast(pointer, C.c_void_p).value
    def collect_list(lst):
        node = lst.head
        while node:
            addr = address(node)
            if addr in owned:
                return
            owned.add(addr)
            item = node.contents
            if item.kind == 0 and item.value.word.data:
                owned.add(item.value.word.data)
            elif item.kind == 1:
                collect_list(item.value.sublist)
            node = item.next
    try:
        backend.api.Error.raise_for_status(sec.SecAccessGetOwnerAndACL(
            access, C.byref(owner), C.byref(count), C.byref(entries)))
        if not owner or not entries:
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
        owned.add(address(owner));owned.add(address(entries))
        collect_list(owner.contents.subject)
        for i in range(count.value):
            p = entries[i].proto
            collect_list(p.subject)
            for ptr in (p.auth.tags, p.validity.start.data, p.validity.end.data):
                if ptr: owned.add(address(ptr))
        if not 1 <= count.value <= 3 or owner.contents.delegate:
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
        head = owner.contents.subject.head.contents
        if head.kind != 2 or head.word_id != 65539 or not head.next:
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
        data = head.next.contents
        if data.kind != 0 or data.next or data.value.word.length != C.sizeof(ProcessSelector):
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
        selector = C.cast(data.value.word.data, C.POINTER(ProcessSelector)).contents
        rows = []
        for i in range(count.value):
            p = entries[i].proto
            if not p.subject.head or p.auth.count != 1 or not p.auth.tags or p.delegate:
                raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
            h = p.subject.head.contents
            row = {"subject":h.word_id,"tag":bytes(p.tag),"authorization":p.auth.tags[0]}
            if h.kind != 2:
                raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
            if include_trusted and h.word_id == 123:
                from .historical_access import code_subjects
                row['code_subjects'] = code_subjects(backend, p.subject)
            if h.word_id == PARTITION_SUBJECT:
                tail = h.next
                if (not tail or tail.contents.kind != 0 or tail.contents.next or
                    address(tail) != address(p.subject.tail)):
                    raise SecureStoreError("JOB_CORE_PARTITION_INVALID")
                d = tail.contents.value.word
                if not d.data or not 1 <= d.length <= 4096:
                    raise SecureStoreError("JOB_CORE_PARTITION_INVALID")
                raw = C.string_at(d.data,d.length)
                if p.auth.tags[0] == INTEGRITY_AUTH:
                    if len(raw) != 32:
                        raise SecureStoreError("JOB_CORE_INTEGRITY_INVALID")
                    row["digest"] = raw.hex()
                else:
                    row["payload"] = plistlib.loads(raw)
            rows.append(row)
        return (selector.version,selector.uid,selector.gid,selector.mask),rows
    finally:
        # Do not free any reference owned by SecAccess or CoreFoundation here.
        # Only this API's copied pointer tree enters owned; shared nodes once.
        libc = C.CDLL("/usr/lib/libSystem.B.dylib")
        libc.free.argtypes = (C.c_void_p,);libc.free.restype = None
        for ptr in owned:
            libc.free(C.c_void_p(ptr))


def verify_core_access(backend, access, cdhash, trusted_raw):
    """Validate the actual stored owner, Core ACL, partition and integrity."""
    return _verify_access(backend, access, cdhash, trusted_raw, stored=True)


def verify_historical_core_access(backend, access, cdhash, trusted_raw, allowed):
    """Only historical read-only callers may accept authenticated successor code."""
    return verify_authenticated_core_access(backend, access, cdhash, trusted_raw, allowed)


def verify_authenticated_core_access(backend, access, cdhash, trusted_raw, allowed):
    """Existing items may retain OS-approved, authenticated Lulu readers.

    The caller's service/account gate still controls which generation it writes.
    New item requests and creation verification must use the strict entrypoints.
    """
    return _verify_access(backend, access, cdhash, trusted_raw, stored=True, allowed=allowed)


def verify_core_request_access(backend, access, trusted_raw):
    """Creation request must have no application-supplied system ACL."""
    return _verify_access(backend, access, None, trusted_raw, stored=False)


def _verify_access(backend, access, cdhash, trusted_raw, *, stored, allowed=None):
    import os
    sec, found = backend.api._sec, backend.api._found
    partition = plistlib.loads(partition_payload(cdhash)) if stored else None
    owner, rows = (_native_structure(backend, access) if allowed is None else
                   _native_structure(backend, access, include_trusted=True))
    if owner != (0x101, os.geteuid(), os.getegid(), 1):
        raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
    historical_apps = None
    if allowed is not None:
        from .historical_access import historical_partition
        partition, _ = historical_partition(rows, cdhash, allowed)
        rows = [dict(row) for row in rows]
        ordinary = [row for row in rows if row.get('authorization') == 1]
        if len(ordinary) != 1:
            raise SecureStoreError('JOB_CORE_ACCESS_INVALID')
        historical_apps = ordinary[0].pop('code_subjects', None)
        application_ids = ({row.get('cdhash') for row in historical_apps}
                           if type(historical_apps) is list else set())
        # The two OS ACLs can remember different authenticated generations.
        # The captured .3 anchor retained .2 in ordinary A but only .3 in P.
        # Validate each set against authenticated code, not against each other;
        # the OS still grants access and this caller still writes only its own
        # generation. Both sets must retain the original identity.
        if (type(historical_apps) is not list or not 1 <= len(historical_apps) <= 1024
                or any(row.get('path') != trusted_raw for row in historical_apps)
                or cdhash not in application_ids or not application_ids <= allowed):
            raise SecureStoreError('JOB_CORE_ACCESS_INVALID')
    expected = [{"subject":123,"tag":b"","authorization":1}]
    digest = None
    if stored:
        integrity = [row for row in rows if row.get("authorization") == INTEGRITY_AUTH]
        if len(integrity) != 1 or re.fullmatch(r"[a-f0-9]{64}", integrity[0].get("digest", "")) is None:
            raise SecureStoreError("JOB_CORE_PARTITION_INVALID")
        digest = integrity[0]["digest"]
        expected += [
            {"subject":PARTITION_SUBJECT,"tag":PARTITION_TAG,"authorization":PARTITION_AUTH,"payload":partition},
            {"subject":PARTITION_SUBJECT,"tag":INTEGRITY_TAG,"authorization":INTEGRITY_AUTH,"digest":digest}]
    if sorted(rows, key=lambda row:row["authorization"]) != expected:
        raise SecureStoreError("JOB_CORE_PARTITION_INVALID")
    sec.SecAccessCopyACLList.restype = C.c_int32
    sec.SecAccessCopyACLList.argtypes = (C.c_void_p, C.POINTER(C.c_void_p))
    refs = C.c_void_p()
    trusted_count = owner_count = partition_count = integrity_count = 0
    try:
        backend.api.Error.raise_for_status(sec.SecAccessCopyACLList(access, C.byref(refs)))
        total = 4 if stored else 2
        if found.CFArrayGetCount(refs) != total:
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
        for i in range(total):
            acl = found.CFArrayGetValueAtIndex(refs, i)
            auth = sec.SecACLCopyAuthorizations(acl)
            apps, label, prompt = C.c_void_p(), C.c_void_p(), C.c_uint32()
            try:
                names = [backend._cfstring_text(found.CFArrayGetValueAtIndex(auth, j))
                         for j in range(found.CFArrayGetCount(auth))]
                if names == ["ACLAuthorizationChangeACL"]:
                    owner_count += 1
                    continue
                backend.api.Error.raise_for_status(sec.SecACLCopyContents(
                    acl, C.byref(apps), C.byref(label), C.byref(prompt)))
                if names == ["ACLAuthorizationIntegrity"]:
                    if not stored or apps.value or not label.value or backend._cfstring_text(label) != digest:
                        raise SecureStoreError("JOB_CORE_INTEGRITY_INVALID")
                    integrity_count += 1
                    continue
                if names == ["ACLAuthorizationPartitionID"]:
                    if apps.value or not label.value:
                        raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
                    try:
                        decoded = plistlib.loads(bytes.fromhex(backend._cfstring_text(label)))
                    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
                        raise SecureStoreError("JOB_CORE_PARTITION_INVALID") from exc
                    if decoded != partition:
                        raise SecureStoreError("JOB_CORE_PARTITION_INVALID")
                    partition_count += 1
                    continue
                if (names != ["ACLAuthorizationAny"] or not apps.value or
                    found.CFArrayGetCount(apps) != (len(historical_apps) if historical_apps is not None else 1)
                    or prompt.value != 0 or any(backend._copy_trusted_application_data(
                        found.CFArrayGetValueAtIndex(apps, j)) != trusted_raw
                        for j in range(found.CFArrayGetCount(apps)))):
                    raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
                trusted_count += 1
            finally:
                for ref in (auth, apps, label):
                    backend._release(ref)
        if (trusted_count, owner_count, partition_count, integrity_count) != (1, 1, int(stored), int(stored)):
            raise SecureStoreError("JOB_CORE_ACCESS_INVALID")
    finally:
        backend._release(refs)
    return {"owner_uid": owner[1], "owner_gid": owner[2], "owner_mode": owner[3],
            "entry_count": len(expected), "trusted_count": trusted_count,
            "partition": partition, "integrity_sha256": digest}
