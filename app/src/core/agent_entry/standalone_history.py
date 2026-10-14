"""Read-only, authenticated history transfer after an App has been replaced.

The system still decides access to old Keychain items. This reader never creates
items, changes access lists, impersonates old code, or repairs an old ledger.
"""
import ctypes as C
import hashlib
import os
from pathlib import Path

from .installed_materials import MATERIALS_DIR, _private_json, _private_raw
from .delivery_identity import MANIFEST_NAME, strict_json
from .task_handover import FILENAME, open_handover, validate_records


def merge_history(older, newer):
    """Union authenticated terminal rows; disagreement never chooses a winner.

    The current predecessor supplies view preferences, while task metadata from
    every retained generation remains available for its historical cards.
    """
    from copy import deepcopy
    from .task_handover_context import validate_context
    rows = {row['job_id']: row for row in older[0]}
    for row in newer[0]:
        if row['job_id'] in rows and row != rows[row['job_id']]:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        rows[row['job_id']] = row
    records = validate_records(list(rows.values()))
    context = deepcopy(newer[1] or older[1])
    if context is not None:
        tasks = deepcopy((older[1] or {}).get('tasks', {}))
        for jid, task in (newer[1] or {}).get('tasks', {}).items():
            if jid in tasks and tasks[jid] != task:
                raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
            tasks[jid] = deepcopy(task)
        context['tasks'] = tasks
        live = deepcopy((older[1] or {}).get('live_recordings', {}))
        for jid, policy in (newer[1] or {}).get('live_recordings', {}).items():
            if jid in live and live[jid] != policy:
                raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
            live[jid] = deepcopy(policy)
        if live:
            context['live_recordings'] = live
        context = validate_context(context, records)
    return records, context

# The published .19 archive was already available locally. Its Core bytes were
# SHA-256 checked against the retained release evidence and codesign verified.
PUBLISHED_CORES = {
    '0b1a1e1c776319c6e5c6d726180ad4e9ae3e942b067efc005403cdecab506f7f':
        '19d61a47ecf27a757c08018a124f0405cbfe18d7',
    # 4.1.2 / 20261010.4: public ZIP/DMG matched the frozen signed App. A
    # preparation failure can leave its user-approved ACL identity without a
    # committed ownership receipt. This pins the exact verified release code.
    '0ebd02056ef505a31c71e7eab0f846f6051e083d4b63fe66ba4da3af09bb7b74':
        '4cb129beaefb32848d0a2ff4244d5f718afc7f8b',
    # 4.1.3 / 20261010.7: all four public assets were fully downloaded and
    # verified. The exact signed Core may remain after failed preparation
    # in a user-approved historical ACL even without a committed installation.
    '0b2ec93c48955ec7d29d41bf0b12e24428240cb2c68e240e28342fc69ba64645':
        '814e3e1e5f7ba3d1cbbb2838a866d7810c89204b',
}


def previous_core_identity(state, owner, *, backup_app=None, known_cores=None):
    raw = _private_raw(Path(state)/MATERIALS_DIR/MANIFEST_NAME)
    if hashlib.sha256(raw).hexdigest() != owner['component_manifest_sha256']:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    manifest = strict_json(raw)
    expected = next((row['sha256'] for row in manifest['components']
                     if row['path'] == 'Contents/MacOS/lulu-agent-core'), None)
    generation = owner.get('core_cdhash') if owner.get('schema_id') == 'lulu.agent-entry-ownership/v4' else PUBLISHED_CORES.get(expected)
    if generation is None and known_cores is not None:
        generation = known_cores.get(expected)
    if generation is None and backup_app is not None:
        from .component_manifest import verify_component_manifest
        from .standalone_source import core_cdhash
        verify_component_manifest(Path(backup_app), Path(state)/MATERIALS_DIR/MANIFEST_NAME)
        generation = core_cdhash(Path(backup_app))
    if generation is None:
        raise RuntimeError('PRODUCT_PREVIOUS_CORE_REQUIRED')
    if known_cores is not None:
        if expected in known_cores and known_cores[expected] != generation:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        known_cores[expected] = generation
    return Path(owner.get('app_path', '/Applications/Lulu.app'))/'Contents/MacOS/lulu-agent-core', generation


def historical_keys(current_keys, old_core, generation, *, known_cores=None):
    from ..cli_runtime.core_task_native import NativeCoreTaskStore, measured_core
    from ..cli_runtime.core_task_keychain import current_core
    from ..cli_runtime.core_task_access import verify_historical_core_access
    from ..license.store import SecureStoreError
    from .standalone_paths import installed_app
    installed_app(Path(old_core).parent.parent.parent)
    current = measured_core(current_core())
    from .task_handover import _validate_source_cores
    known = _validate_source_cores(known_cores or {})
    allowed = frozenset({generation, current, *known.values(), *PUBLISHED_CORES.values()})

    class Reader(NativeCoreTaskStore):
        def __init__(self):
            self.backend = current_keys._keyring()
            self.service = current_keys.service + '.CoreTasks.' + generation
            self.core, self.cdhash = Path(old_core), generation

        def _verify_item(self, item):
            if not item.value:
                raise SecureStoreError('JOB_CORE_ITEM_REFERENCE_REQUIRED')
            access = C.c_void_p()
            try:
                self.backend.api.Error.raise_for_status(
                    self.backend.api._sec.SecKeychainItemCopyAccess(item, C.byref(access)))
                return verify_historical_core_access(self.backend, access, self.cdhash,
                    os.fsencode(self.core)+b'\0', allowed)
            finally:
                self.backend._release(access)

        def create(self, *args):
            raise SecureStoreError('HISTORICAL_STORAGE_READ_ONLY')

        def replace(self, *args):
            raise SecureStoreError('HISTORICAL_STORAGE_READ_ONLY')

    reader = Reader()
    class Keys:
        def get_text(self, name):
            from ..cli_runtime.core_task_keychain import _assert_core_account
            _assert_core_account(name)
            try:
                return reader.read(name)
            except Exception as exc:
                # Record only the closed item role, never the account, stored
                # value, scope, path or ACL. Registry/key/anchor can differ.
                exc.lulu_storage_item_kind = ('core_registry' if name == 'lulu_core_job_registry_v1'
                    else 'core_key' if name.startswith('lulu_core_job_key_v1:') else 'core_anchor')
                raise
    return Keys()


def verified_task_snapshot(root, installation_id, keys):
    from ..job_ledger.storage_binding import (REGISTRY_KEY, KEY_PREFIX, ANCHOR_PREFIX,
        _validate_registry, _CoreAnchor, _read, _json)
    from ..job_ledger.store import verify_existing_ledger
    import hmac
    raw = keys.get_text(REGISTRY_KEY)
    if raw is None:
        if os.path.lexists(root):
            from ..job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode
            _validate_no_follow_ancestors(root)
            _validate_owned_mode(root, mode=0o700, directory=True)
            # Core creates this directory before its first secure-store read.
            # Interrupted authorization can therefore leave an unused empty
            # generation. Existing files or orphaned secrets remain a hard stop.
            if (any(root.iterdir()) or any(keys.get_text(prefix+'core-'+installation_id)
                    is not None for prefix in (KEY_PREFIX, ANCHOR_PREFIX))
                    or any(root.iterdir())):
                raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        return {'jobs': [], 'events': {}, 'receipt_inheritances': []}
    record = _validate_registry(raw, root, installation_id)
    if record['body']['scheme'] != 'core-v1':
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    if record['phase'] == 'INTENT':
        _verified_unstarted_rows(root, installation_id, keys, record)
        return {'jobs': [], 'events': {}, 'receipt_inheritances': []}
    anchor = _CoreAnchor(keys, record['body'])
    intent = (_json(record['body'])+'\n').encode()
    binding = (_json({'body': record['body'], 'hmac': hmac.new(anchor.hmac_key(), intent, hashlib.sha256).hexdigest()})+'\n').encode()
    if (_read(root/'task-integrity-v1/intent.json') != intent
            or _read(root/'task-integrity-v1/binding.json') != binding
            or hashlib.sha256(binding).hexdigest() != record['binding_sha256']):
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    proof = verify_existing_ledger(root/'jobs', anchor, include_records=True)
    snapshot = _private_raw(root/'jobs/job-ledger-v1/snapshot.json')
    if hashlib.sha256(snapshot).hexdigest() != proof['snapshot_sha256']:
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    return proof['records']


def rows_from_jobs(jobs):
    rows = []
    for job in jobs:
        attempt = next(a for a in job['attempts'] if a['attempt_id'] == job['current_attempt_id'])
        rows.append({name: job[name] for name in ('job_id', 'job_kind', 'job_revision', 'state',
                                                 'result_ref', 'partial_failures')} | {
            'lease_epoch': attempt['lease_epoch'], 'last_sequence': job['event_sequence'],
            'receipt_ref': f"lulu-job:{job['job_id']}:revision:{job['job_revision']}"})
    return rows


def verified_terminal_rows(root, installation_id, keys):
    return validate_records(rows_from_jobs(verified_task_snapshot(root, installation_id, keys)['jobs']))


def _verified_unstarted_rows(root, installation_id, keys, record):
    """Recognize only authenticated, empty first-start prefixes; never repair.

    Business runtime is unavailable until the registry reaches READY. Retain
    partial setup for its own Core to resume, without blocking the rollback.
    """
    from ..job_ledger.storage_binding import (REGISTRY_KEY, KEY_PREFIX,
        _validate_registry, _CoreAnchor, _read, _json)
    from ..job_ledger.store import (verify_existing_ledger,
        _validate_no_follow_ancestors, _validate_owned_mode)
    from .product_transaction import snapshot_digest
    import hmac
    def reject(): raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    def directory(path, allowed):
        _validate_no_follow_ancestors(path)
        _validate_owned_mode(path, mode=0o700, directory=True)
        names = {p.name for p in path.iterdir()}
        if names - allowed: reject()
        return names
    before = snapshot_digest(root)
    names = directory(root, {'task-integrity-v1', 'jobs'})
    binding_root = root/'task-integrity-v1'
    members = directory(binding_root, {'intent.json', 'binding.json', 'prepared-jobs'}) if 'task-integrity-v1' in names else set()
    intent = (_json(record['body'])+'\n').encode()
    if 'intent.json' in members and _read(binding_root/'intent.json') != intent: reject()
    anchor = _CoreAnchor(keys, record['body'])
    key_exists = keys.get_text(KEY_PREFIX+record['body']['scope']) is not None
    head = anchor.load()
    ledgers = ([root/'jobs'] if 'jobs' in names else []) + (
        [binding_root/'prepared-jobs'] if 'prepared-jobs' in members else [])
    if len(ledgers) > 1 or (not ledgers and head is not None): reject()
    if not key_exists:
        if 'binding.json' in members or ledgers or head is not None: reject()
    else:
        if 'intent.json' not in members: reject()
        key = anchor.hmac_key()
        binding = (_json({'body': record['body'],
            'hmac': hmac.new(key, intent, hashlib.sha256).hexdigest()})+'\n').encode()
        if 'binding.json' in members and _read(binding_root/'binding.json') != binding: reject()
        for ledger in ledgers:
            if 'binding.json' not in members: reject()
            if directory(ledger, {'job-ledger-v1'}) != {'job-ledger-v1'}: reject()
            if directory(ledger/'job-ledger-v1', {'journal.jsonl', 'snapshot.json', 'transaction.lock'}) != {'journal.jsonl', 'snapshot.json', 'transaction.lock'}: reject()
            proof = verify_existing_ledger(ledger, anchor)
            if proof['job_count'] or proof['head']['sequence'] or proof['head']['head_hmac'] != '0'*64: reject()
    if (_validate_registry(keys.get_text(REGISTRY_KEY), root, installation_id) != record
            or anchor.load() != head or snapshot_digest(root) != before):
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    return []


def capture_history(state, owner, *, product_root, keys, backup_app=None, key_reader=None, known_cores=None):
    key_reader = key_reader or (lambda keys, core, generation:
        historical_keys(keys, core, generation, known_cores=known_cores))
    old_core, generation = previous_core_identity(state, owner, backup_app=backup_app, known_cores=known_cores)
    def current_history(cached=None, *, task_unchanged=False):
        handover_path = Path(state)/MATERIALS_DIR/FILENAME
        previous, context = [], None
        if os.path.lexists(handover_path):
            value = _private_json(handover_path)
            previous = open_handover(value, keys=keys, owner=owner, product_root=product_root)
            context = value['body'].get('gui_context')
        root = Path(product_root)/'task-generations'/generation
        if task_unchanged:
            # The authenticated checkpoint still proves these exact native rows.
            rows = cached[0]
        elif root.exists():
            native_keys = key_reader(keys, old_core, generation)
            if cached is not None:
                from ..job_ledger.storage_binding import REGISTRY_KEY
                # A stale established checkpoint must not become an empty,
                # supposedly never-initialized ledger after files/keys disappear.
                if native_keys.get_text(REGISTRY_KEY) is None:
                    raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
            rows = verified_terminal_rows(root, owner['installation_id'], native_keys)
        else:
            # A never-started previous generation has only its signed handover.
            rows = []
        merged = {row['job_id']: row for row in previous}
        for row in rows:
            if row['job_id'] in merged and row != merged[row['job_id']]:
                raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
            merged[row['job_id']] = row
        rows = validate_records(list(merged.values()))
        from .task_handover_context import capture_context
        result = rows, capture_context(Path(product_root), old_core.parent.parent.parent,
            rows, context, generation=generation)
        return merge_history(cached, result) if cached is not None else result

    from .history_checkpoint import read_checkpoint
    checkpoint = read_checkpoint(product_root=product_root, owner=owner,
        generation=generation, keys=keys, state=state, refresh=current_history)
    if checkpoint is not None:
        return checkpoint
    return current_history()
