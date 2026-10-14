"""Decode stored code identities using public Security APIs, without Keychain IO.

SecTrustedApplicationCopyData is only a path. The actual code requirement is in
the public CSSM subject returned by SecAccessGetOwnerAndACL (Apple ACL.cpp and
acl_codesigning.cpp). Accept only the exact ad-hoc CDHash requirement we issue.
"""
import ctypes as C
import re
import struct

from ..license.store import SecureStoreError


def _invalid(detail=None):
    error = SecureStoreError('JOB_CORE_ACCESS_INVALID')
    if detail is not None:
        error.__cause__ = SecureStoreError('JOB_CORE_HISTORICAL_'+detail)
    return error


def _nodes(subject):
    nodes, seen = [], set()
    node = subject.head
    if subject.kind != 0:
        raise _invalid()
    while node:
        address = C.cast(node, C.c_void_p).value
        if address in seen or len(nodes) >= 1028:
            raise _invalid()
        seen.add(address)
        nodes.append(node.contents)
        last = address
        node = node.contents.next
    if not nodes or last != C.cast(subject.tail, C.c_void_p).value:
        raise _invalid()
    return nodes


def _word(node, expected):
    if node.kind != 2 or node.word_id != expected:
        raise _invalid()


def _bytes(node, maximum=65536):
    if node.kind != 0:
        raise _invalid()
    data = node.value.word
    if not data.data or not 1 <= data.length <= maximum:
        raise _invalid()
    return C.string_at(data.data, data.length)


def requirement_cdhash(backend, raw):
    """Use public requirement decoding; paths and legacy hashes grant nothing."""
    sec = backend.api._sec
    sec.SecRequirementCreateWithData.restype = C.c_int32
    sec.SecRequirementCreateWithData.argtypes = (C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p))
    sec.SecRequirementCopyString.restype = C.c_int32
    sec.SecRequirementCopyString.argtypes = (C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p))
    data, requirement, text = None, C.c_void_p(), C.c_void_p()
    try:
        data = backend._bytes_data(raw)
        if (not data or sec.SecRequirementCreateWithData(data, 0, C.byref(requirement))
                or not requirement.value or sec.SecRequirementCopyString(requirement, 0, C.byref(text))
                or not text.value):
            raise _invalid()
        match = re.fullmatch(r'cdhash H"([a-fA-F0-9]{40})"', backend._cfstring_text(text))
        if match is None:
            raise _invalid()
        return match[1].lower()
    finally:
        for ref in (text, requirement, data):
            backend._release(ref)


def code_subjects(backend, subject):
    nodes = _nodes(subject)
    if len(nodes) < 5:
        raise _invalid()
    _word(nodes[0], 123)  # threshold: one of n applications or the system prompt
    _word(nodes[1], 1)
    _word(nodes[2], len(nodes) - 3)
    result = []
    for node in nodes[3:-1]:
        if node.kind != 1:
            raise _invalid()
        code = _nodes(node.value.sublist)
        if len(code) != 5:
            raise _invalid()  # no missing requirement, extra subject or opaque auxiliary data
        _word(code[0], 116)
        _word(code[1], 1)
        if len(_bytes(code[2], 20)) != 20:
            raise _invalid()
        path = _bytes(code[3], 4096)
        if not path.startswith(b'/') or not path.endswith(b'\0') or b'\0' in path[:-1]:
            raise _invalid()
        result.append({'path': path, 'cdhash': requirement_cdhash(backend, _bytes(code[4]))})
    if nodes[-1].kind != 1:
        raise _invalid()
    prompt = _nodes(nodes[-1].value.sublist)
    if len(prompt) != 3:
        raise _invalid()
    _word(prompt[0], 65536)
    if _bytes(prompt[1], 4) != struct.pack('=HH', 0x101, 0):
        raise _invalid()
    _bytes(prompt[2], 4096)
    return result


def historical_partition(rows, original, allowed):
    if (type(allowed) is not frozenset or not 1 <= len(allowed) <= 1002
            or any(type(v) is not str or re.fullmatch(r'[a-f0-9]{40}', v) is None for v in allowed)
            or original not in allowed):
        raise _invalid('ALLOWLIST_INVALID')
    values = [row.get('payload') for row in rows if row.get('authorization') == 65538]
    if len(values) != 1 or type(values[0]) is not dict or set(values[0]) != {'Partitions'}:
        raise _invalid('PARTITION_STRUCTURE_INVALID')
    partition = values[0]
    names = partition['Partitions']
    if type(names) is not list or not 1 <= len(names) <= 1024:
        raise _invalid('PARTITION_COUNT_INVALID')
    if any(type(v) is not str or re.fullmatch(r'cdhash:[a-f0-9]{40}', v) is None for v in names):
        raise _invalid('PARTITION_FORMAT_INVALID')
    # securityd appends an identity when remembering an authorization. Repeated
    # entries grant no additional authority; bound the raw list, then validate
    # every distinct identity against authenticated code. Preserve the original
    # payload for the independent native ACL comparison.
    identities = {v[7:] for v in names}
    if original not in identities:
        raise _invalid('ORIGINAL_IDENTITY_MISSING')
    if not identities <= allowed:
        raise _invalid('UNKNOWN_IDENTITY')
    return partition, identities
