"""Stable local task storage and one-time authenticated legacy inheritance.

Normal preparation never constructs a system credential backend. Legacy source
files are authenticated read-only and kept in place; each imported job retains
its complete events, attempts, receipts, policy and result references.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from copy import deepcopy
from contextlib import contextmanager
from time import sleep as _idle_sleep

from ..local_integrity import (product_local_store, SCHEME, TASK_DIRECTORY,
    ID_KEY, MIGRATION_KEY, AUTO_MIGRATION_KEY, HISTORY_KEY)
from .installed_materials import _private_json, MATERIALS_DIR
from .ownership import verify_ownership_receipt, verify_ready_action_receipt
from .task_handover import FILENAME, open_handover, validate_records, TERMINAL
from .task_handover_context import validate_context, capture_context

MIGRATION_FAILURES = {
    'PRODUCT_UNFINISHED_TASKS', 'PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS',
    'PRODUCT_PREVIOUS_CORE_REQUIRED', 'PRODUCT_TASKS_CHANGED', 'JOB_IMPORT_CONFLICT',
    'JOB_CORE_KEY_MISSING', 'JOB_LEDGER_HMAC_INVALID', 'JOB_BINDING_FILE_CHANGED',
    'JOB_LEDGER_HIGH_WATER_WRITE_UNCERTAIN', 'LOCAL_INTEGRITY_BUSY',
    'JOB_LEDGER_HIGH_WATER_ROLLBACK',
    'JOB_CORE_ACCESS_INVALID',
}
LEGACY_ROOTS = ('jobs', 'task-integrity-v1', 'task-generations', 'AgentEntry/app-generations',
    'AgentEntry/ownership.json', 'AgentEntry/attempted-cores.json')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _authenticated_local_readers(app, product, keys, installation_id):
    """Recognize committed successor code remembered by the OS in old ACLs."""
    from .standalone_history import previous_core_identity
    from ..job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode
    root = Path(product)/'AgentEntry/local-app-generations'
    if not os.path.lexists(root): return {}
    _validate_no_follow_ancestors(root)
    _validate_owned_mode(root, mode=0o700, directory=True)
    readers = {}
    for state in root.iterdir():
        if not re.fullmatch(r'[a-f0-9]{64}', state.name):
            continue  # The namespace also contains its lock and staging files.
        _validate_no_follow_ancestors(state)
        _validate_owned_mode(state, mode=0o700, directory=True)
        owner = verify_ownership_receipt(state/'ownership.json', key_store=keys)
        verify_ready_action_receipt(state/'current.json', ownership=owner)
        if owner['installation_id'] != installation_id or owner.get('app_path') != str(app):
            continue
        # This checks the manifest against the authenticated ownership receipt.
        # These identities allow only historical reads, never an old task source.
        previous_core_identity(state, owner, known_cores=readers)
    return readers


def _legacy_shared_present(product):
    root = Path(product)/'jobs'
    if not os.path.lexists(root): return False
    if not root.is_dir() or root.is_symlink(): return True
    # New local tasks share this result vault; it is not an old ledger.
    return any(path.name != 'results' for path in root.iterdir())


def legacy_present(product):
    product = Path(product)
    return _legacy_shared_present(product) or any(os.path.lexists(product/name) for name in (
        'task-generations', 'AgentEntry/ownership.json', 'AgentEntry/app-generations'))


def _legacy_id_hint(product):
    """A locator only. Explicit migration independently authenticates the owner."""
    root = Path(product)/'AgentEntry'
    paths = [root/'ownership.json'] if os.path.lexists(root/'ownership.json') else []
    generations = root/'app-generations'
    if os.path.lexists(generations):
        from ..job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode
        _validate_no_follow_ancestors(generations)
        _validate_owned_mode(generations, mode=0o700, directory=True)
        paths += [p/'ownership.json' for p in generations.iterdir()
                  if re.fullmatch(r'[a-f0-9]{64}', p.name)]
    ids = set()
    for path in paths:
        from ..job_ledger.store import _validate_no_follow_ancestors
        _validate_no_follow_ancestors(path)
        owner = _private_json(path)
        value = owner.get('installation_id')
        if (type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{7,150}', value)
                or owner.get('installation_id_digest') != hashlib.sha256(value.encode()).hexdigest()):
            raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
        ids.add(value)
    if len(ids) > 1:
        raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
    return next(iter(ids), None)


def initialize_local_identity(product, keys):
    generations = Path(product)/'AgentEntry/local-app-generations'
    if os.path.lexists(generations):
        from ..job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode
        _validate_no_follow_ancestors(generations)
        _validate_owned_mode(generations, mode=0o700, directory=True)
    established = os.path.lexists(generations) and any(re.fullmatch(r'[a-f0-9]{64}', p.name) for p in generations.iterdir())
    value = keys.get_text(ID_KEY)
    if value is None:
        if (os.path.lexists(Path(product)/TASK_DIRECTORY)
                or established):
            raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
        try:
            value = _legacy_id_hint(product)
        except Exception:
            # Broken legacy metadata must not prevent an independent new task.
            # No failure here is interpreted as a verified empty old history.
            value = None
        value = value or 'app-'+secrets.token_hex(32)
        keys.write_new(ID_KEY, value)
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{7,150}', value):
        raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    keys.installation_id = value
    from .ownership import _key_name
    if (established
            and keys.get_text(_key_name(hashlib.sha256(value.encode()).hexdigest())) is None):
        raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    if keys.get_text(MIGRATION_KEY) is None:
        keys.write_new(MIGRATION_KEY, _encoded({'state': 'pending' if legacy_present(product) else 'none'}))
    return value


def read_local_history(keys):
    raw = keys.get_text(HISTORY_KEY)
    if raw is None:
        return [], None
    from .delivery_identity import strict_json
    value = strict_json(raw.encode())
    if type(value) is not dict or set(value) != {'records', 'context'}:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    records = validate_records(value['records'])
    context = value['context']
    if context is not None:
        context = validate_context(context, records)
    return records, context


def without_imported_history(history, job_ids):
    records = [row for row in history[0] if row['job_id'] not in job_ids]
    context = deepcopy(history[1])
    if context is not None:
        known = {row['job_id'] for row in records}
        context['tasks'] = {jid: value for jid, value in context['tasks'].items() if jid in known}
        if 'live_recordings' in context:
            context['live_recordings'] = {jid: value for jid, value in context['live_recordings'].items() if jid in known}
        context['view']['hidden_ids'] = [jid for jid in context['view']['hidden_ids'] if jid in known]
        for group in context['view']['groups']:
            group['members'] = [m for m in group['members'] if 'task_id' not in m or m['task_id'] in known]
        context = validate_context(context, records)
    return records, context


def migration_status(product):
    """Read-only GUI hint; never creates files or reads system secrets."""
    root = Path(product)/SCHEME
    if not os.path.lexists(root):
        return 'pending' if legacy_present(product) else 'none'
    from ..local_integrity import LocalIntegrityStore
    keys = LocalIntegrityStore(root)
    raw = keys.get_text(MIGRATION_KEY)
    from .delivery_identity import strict_json
    value = strict_json(raw.encode()) if raw is not None else {'state': 'pending'}
    if type(value) is not dict or set(value) != {'state'} or value['state'] not in {'pending', 'partial', 'complete', 'failed', 'none'}:
        raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    return value['state']


def automatic_migration_needed(product):
    """Local-only decision; a past denial/interruption never prompts on reopen."""
    status = migration_status(product)
    from ..local_integrity import LocalIntegrityStore
    root = Path(product)/SCHEME
    raw = LocalIntegrityStore(root).get_text(AUTO_MIGRATION_KEY) if os.path.lexists(root) else None
    if raw is None:
        return status in {'pending', 'failed'} or (status == 'none' and legacy_present(product))
    from .delivery_identity import strict_json
    value = strict_json(raw.encode())
    if (type(value) is not dict or set(value) not in ({'sources'}, {'kind', 'sources'})
            or type(value['sources']) is not str or not re.fullmatch(r'[a-f0-9]{64}', value['sources'])):
        raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    if value.get('kind') not in (None, 'legacy-task-sources-v2'):
        raise RuntimeError('LOCAL_INTEGRITY_RECOVERY_REQUIRED')
    if 'kind' not in value:
        # Earlier private builds included current results in their marker.
        # Preparation upgrades that hint locally; never reread OS secrets here.
        return False
    # A deliberate rollback may produce genuinely new old-format records.
    # Metadata is a change hint only; import still authenticates all content.
    return value['sources'] != _legacy_metadata_fingerprint(product)


def _legacy_metadata_fingerprint(product):
    import stat
    from ..job_ledger.store import _validate_no_follow_ancestors
    from .product_transaction import snapshot_digest
    rows = []
    for name in LEGACY_ROOTS:
        root = Path(product)/name
        if not os.path.lexists(root): continue
        _validate_no_follow_ancestors(root)
        pending = [root]
        while pending:
            path = pending.pop()
            _validate_no_follow_ancestors(path)
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RuntimeError('PRODUCT_PATH_UNSAFE')
            if stat.S_ISDIR(info.st_mode): pending.extend(path.iterdir())
        # Inodes and filesystem times change after identical file restores.
        # Reuse the protected content projection;
        # actual old records still trigger authentication.
        if name == 'jobs' and root.is_dir():
            children = [(p.name, snapshot_digest(p)) for p in root.iterdir() if p.name != 'results']
            if children:
                rows.append((name, stat.S_IMODE(root.lstat().st_mode), sorted(children)))
        else:
            rows.append((name, snapshot_digest(root)))
    return hashlib.sha256(_encoded(sorted(rows)).encode()).hexdigest()


def prepare_local_app(app, product):
    from .standalone_state import prepare_install_state
    keys = product_local_store(product)
    initialize_local_identity(product, keys)
    raw = keys.get_text(AUTO_MIGRATION_KEY)
    if raw is not None:
        from .delivery_identity import strict_json
        previous = strict_json(raw.encode())
        if (type(previous) is dict and set(previous) == {'sources'}
                and type(previous['sources']) is str and re.fullmatch(r'[a-f0-9]{64}', previous['sources'])):
            # This one-time marker conversion is a local change hint only.
            # Preserve the compatibility outcome and every original archive.
            keys.set_text(AUTO_MIGRATION_KEY, _encoded({'kind':'legacy-task-sources-v2',
                'sources':_legacy_metadata_fingerprint(product)}))
    # Stable jobs/history are independent of any particular App generation.
    # Each App still receives its own signed-code/ownership receipt.
    return prepare_install_state(app, product_root=product, keys=keys, verify_existing=False)


def merge_views(current, old, known):
    """Preserve new preferences and remap colliding legacy folder numbers."""
    result = deepcopy(current)
    used = {g['id'] for g in result['groups']}
    members = {m['task_id'] for g in result['groups'] for m in g['members'] if 'task_id' in m}
    skipped = {_encoded(m) for g in result['groups'] for m in g['members'] if 'task_id' not in m}
    for original in old['groups']:
        group = deepcopy(original)
        group['members'] = [m for m in group['members'] if
            (_encoded(m) not in skipped if 'task_id' not in m else
             m['task_id'] in known and m['task_id'] not in members)]
        if not group['members'] and (original['members'] or any(
                {k: v for k, v in g.items() if k != 'id'} == {k: v for k, v in group.items() if k != 'id'}
                for g in result['groups'])): continue
        if group['id'] in used:
            group['id'] = max(used, default=0)+1
        used.add(group['id'])
        members.update(m['task_id'] for m in group['members'] if 'task_id' in m)
        skipped.update(_encoded(m) for m in group['members'] if 'task_id' not in m)
        result['groups'].append(group)
    result['hidden_ids'] = sorted((set(current['hidden_ids']) | set(old['hidden_ids'])) & known)
    return result


def _assert_legacy_idle(snapshot, view, app, old_apps):
    from ..gui_task_view import GuiTaskViewStore
    running_ids = {j['job_id'] for j in snapshot['jobs'] if j['state'] in {'running', 'cancel_requested'}}
    if not running_ids:
        return
    if any(pid is not None and not GuiTaskViewStore.owner_has_exited(pid)
           for jid, pid in view['owners'].items() if jid in running_ids):
        raise RuntimeError('PRODUCT_UNFINISHED_TASKS')
    from ..updater.handover import _assert_old_core_stopped
    for old_app in {Path(path) for path in old_apps}:
        if old_app != Path(app):
            _assert_old_core_stopped(old_app)


@contextmanager
def _migration_barrier(product, installation_id, keys, on_storage_changed):
    from ..updater.handover import handover_lock
    from .product_transaction import snapshot_digest
    from .standalone_history import verified_task_snapshot
    def snapshot():
        return tuple(snapshot_digest(Path(product)/name) for name in (TASK_DIRECTORY, SCHEME))
    with handover_lock(Path(product)/'updates', exclusive=True):
        root = Path(product)/TASK_DIRECTORY
        if os.path.lexists(root):
            current = verified_task_snapshot(root, installation_id, keys)
            if any(j['state'] in {'running', 'cancel_requested'} for j in current['jobs']):
                raise RuntimeError('PRODUCT_UNFINISHED_TASKS')
        before = snapshot()
        try:
            yield
        finally:
            if on_storage_changed is not None and snapshot() != before:
                # Reopen the service before writers can enter, even if only
                # part of an import committed before a disk/authorization error.
                on_storage_changed()


@contextmanager
def _import_lock(product):
    """Only serializes inheritance helpers; ordinary business never takes it."""
    import fcntl
    import stat
    from ..updater.secure_io import ensure_private_dir
    directory = ensure_private_dir(Path(product)/'updates/transactions')
    fd = os.open(directory/'legacy-import.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('LOCAL_INTEGRITY_BUSY') from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def _idle_import_barrier(product, installation_id, keys, on_storage_changed, *, wait_for_idle):
    """Wait without re-reading OS credentials or holding a business lock."""
    from ..updater.models import UpdateError
    while True:
        guard = _migration_barrier(product, installation_id, keys, on_storage_changed)
        try:
            guard.__enter__()
        except Exception as exc:
            busy = ((isinstance(exc, UpdateError) and exc.code == 'handover_busy')
                    or str(exc) == 'PRODUCT_UNFINISHED_TASKS')
            if not wait_for_idle or not busy: raise
            _idle_sleep(.5)
        else:
            break
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


def migrate_legacy_tasks(app, product, *, legacy_keys=None, key_reader=None,
        on_storage_changed=None, automatic=False, wait_for_idle=False):
    """Authenticate old originals without blocking new tasks, then commit while idle.

    A durable attempt marker precedes OS authorization. Denial or interruption
    never causes another automatic prompt; only an explicit exception retry can
    read old credentials again. Source keys and files remain read-only.
    """
    with _import_lock(product):
        return _inherit_legacy_tasks(app, product, legacy_keys=legacy_keys, key_reader=key_reader,
            on_storage_changed=on_storage_changed, automatic=automatic, wait_for_idle=wait_for_idle)


def _inherit_legacy_tasks(app, product, *, legacy_keys, key_reader,
        on_storage_changed, automatic, wait_for_idle):
    from .standalone_state import install_state_root, verify_install_state
    from .standalone_runtime import previous_states
    from .standalone_history import (previous_core_identity, historical_keys,
        verified_task_snapshot, rows_from_jobs, merge_history)
    from .product_transaction import snapshot_digest
    from ..job_ledger.storage_binding import prepare_core_task_store, _private_directory
    from ..gui_task_view import GuiTaskViewStore
    keys = product_local_store(product)
    initialize_local_identity(product, keys)
    state = install_state_root(app, product)
    local_owner, _ = verify_install_state(app, state_root=state, keys=keys)
    if automatic and not automatic_migration_needed(product):
        return 0
    # Persist the exception state first, including a killed authorization child.
    keys.set_text(MIGRATION_KEY, _encoded({'state': 'failed'}))
    keys.set_text(AUTO_MIGRATION_KEY, _encoded({'kind':'legacy-task-sources-v2',
        'sources': _legacy_metadata_fingerprint(product)}))
    task_root = Path(product)/TASK_DIRECTORY
    before = {name: snapshot_digest(Path(product)/name) for name in LEGACY_ROOTS}
    try:
        if legacy_keys is None:
            from ..cli_runtime.core_task_keychain import CoreTaskKeyringStore
            legacy_keys = CoreTaskKeyringStore()
        sources = previous_states(product, target=state, keys=legacy_keys)
        known_cores = {}
        owners, history = [], ([], None)
        for source in sources:
            owner = verify_ownership_receipt(source/'ownership.json', key_store=legacy_keys)
            verify_ready_action_receipt(source/'current.json', ownership=owner)
            if owner['installation_id'] != local_owner['installation_id']:
                raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
            owners.append((source, owner))
            path = source/MATERIALS_DIR/FILENAME
            if os.path.lexists(path):
                value = _private_json(path)
                records = open_handover(value, keys=legacy_keys, owner=owner, product_root=product)
                history = merge_history(history, (records, value['body'].get('gui_context')))
                for digest, generation in value['body'].get('source_cores', {}).items():
                    if digest in known_cores and known_cores[digest] != generation:
                        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
                    known_cores[digest] = generation
        if owners:
            from .attempted_cores import read_attempted_cores
            for digest, generation in read_attempted_cores(product, owners[-1][1], legacy_keys).items():
                if digest in known_cores and known_cores[digest] != generation:
                    raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
                known_cores[digest] = generation
        identities = [previous_core_identity(source, owner, known_cores=known_cores)
                      for source, owner in owners]
        reader_cores = dict(known_cores)
        for digest, generation in _authenticated_local_readers(
                app, product, keys, local_owner['installation_id']).items():
            if digest in reader_cores and reader_cores[digest] != generation:
                raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
            reader_cores[digest] = generation
        generations = {generation: (core, owner) for (source, owner), (core, generation) in zip(owners, identities)}
        snapshots, old_views = [], []
        # Include retained generations authenticated by a signed handover,
        # including a failed candidate without a committed App receipt.
        generations.update({generation: (Path(app)/'Contents/MacOS/lulu-agent-core', owners[-1][1])
            for generation in known_cores.values() if generation not in generations} if owners else {})
        retained = Path(product)/'task-generations'
        unresolved = False
        if os.path.lexists(retained):
            from ..job_ledger.store import _validate_no_follow_ancestors, _validate_owned_mode
            _validate_no_follow_ancestors(retained)
            _validate_owned_mode(retained, mode=0o700, directory=True)
            for path in retained.iterdir():
                if not re.fullmatch(r'[a-f0-9]{40}', path.name):
                    raise RuntimeError('PRODUCT_PREVIOUS_CORE_REQUIRED')
                _validate_no_follow_ancestors(path)
                _validate_owned_mode(path, mode=0o700, directory=True)
                # Do not read an unauthenticated archive as a task source.
                # Its already-authenticated handover cards may still be
                # restored independently; keep the archive and report a
                # partial result rather than blocking every verified row.
                unresolved = unresolved or path.name not in generations
        for generation, (core, owner) in generations.items():
            root = Path(product)/'task-generations'/generation
            if not os.path.lexists(root): continue
            reader = (key_reader(legacy_keys, core, generation) if key_reader else
                      historical_keys(legacy_keys, core, generation, known_cores=reader_cores))
            try:
                snapshot = verified_task_snapshot(root, owner['installation_id'], reader)
            except Exception as exc:
                from keyring.backends.macOS.api import KeychainDenied
                if isinstance(exc, KeychainDenied):
                    # Stop further secret reads after OS cancellation. The
                    # handovers above were independently authenticated already;
                    # keep them while retaining every unread raw archive.
                    unresolved = True
                    break
                from ..job_ledger.contracts import JobLedgerError
                if not isinstance(exc, JobLedgerError) or exc.reason_code != 'JOB_LEDGER_HIGH_WATER_ROLLBACK':
                    raise
                # An obsolete raw archive cannot override its newer anchor.
                # Keep it untouched; independently sealed handover cards are
                # already authenticated and remain usable as read-only history.
                unresolved = True
                continue
            view = GuiTaskViewStore(root/'jobs').load()
            _assert_legacy_idle(snapshot, view, app, [owner.get('app_path', '/Applications/Lulu.app')])
            snapshots.append((generation, snapshot))
            terminal = [j for j in snapshot['jobs'] if j['state'] in TERMINAL]
            context = capture_context(Path(product), core.parent.parent.parent,
                rows_from_jobs(terminal), generation=generation)
            # Actual imported jobs own their full metadata. Only terminal
            # projections need historical live-folder input hints.
            history = merge_history(history, (validate_records(rows_from_jobs(terminal)), context))
            old_views.append({'groups': view['groups'], 'hidden_ids': view['hidden_ids']})
        if _legacy_shared_present(product):
            # Early shared-ledger versions are verified through their
            # original scoped anchor; never initialized or repaired here.
            from ..job_ledger.storage_preflight import inspect_task_storage, authenticate_legacy_scope
            from ..cli_runtime.keychain import KeyringJobIntegrityAnchorStore
            from ..job_ledger.store import verify_existing_ledger
            factory = lambda scope: KeyringJobIntegrityAnchorStore(legacy_keys, scope)
            scope, _ = authenticate_legacy_scope(Path(product), inspect_task_storage(Path(product)), factory)
            proof = verify_existing_ledger(Path(product)/'jobs', factory(scope), include_records=True)
            view = GuiTaskViewStore(Path(product)/'jobs').load()
            _assert_legacy_idle(proof['records'], view, app,
                [owner.get('app_path', '/Applications/Lulu.app') for _, owner in owners]
                or ['/Applications/Lulu.app'])
            snapshots.insert(0, (hashlib.sha256(scope.encode()).hexdigest(), proof['records']))
            old_views.insert(0, {'groups': view['groups'], 'hidden_ids': view['hidden_ids']})
        if legacy_present(product) and not owners and not snapshots:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_REQUIRED')
        with _idle_import_barrier(product, local_owner['installation_id'], keys,
                on_storage_changed, wait_for_idle=wait_for_idle):
            if any(snapshot_digest(Path(product)/name) != value for name, value in before.items()):
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            verify_install_state(app, state_root=state, keys=keys)
            history = merge_history(read_local_history(keys), history)
            _private_directory(task_root)
            store = prepare_core_task_store(task_root, local_owner['installation_id'], keys, legacy_storage=False)
            imported = 0
            present_ids = {j.job_id for j in store.list_jobs()}
            for source, snapshot in snapshots:
                for job in snapshot['jobs']:
                    try:
                        imported += store.import_verified_job(source=source, job=job,
                            events=snapshot['events'][job['job_id']], inheritances=[i for i in
                                snapshot['receipt_inheritances'] if i['job_id'] == job['job_id']])
                        present_ids.add(job['job_id'])
                    except Exception as exc:
                        from ..job_ledger.contracts import JobLedgerError
                        if (not isinstance(exc, JobLedgerError) or exc.reason_code != 'JOB_IMPORT_CONFLICT'
                                or job['job_id'] not in present_ids):
                            raise
                        # Two authenticated generations can reuse an ID for
                        # different task records. Keep the current record and
                        # every original, never rename or overwrite either.
                        unresolved = True
            from datetime import datetime, timezone
            store.recover_orphaned_running(changed_at=datetime.now(timezone.utc).isoformat())
            view_store = GuiTaskViewStore(task_root/'jobs')
            view = view_store.load()
            merged = {'groups': view['groups'], 'hidden_ids': view['hidden_ids']}
            known = {j.job_id for j in store.list_jobs()} | {r['job_id'] for r in history[0]}
            for old in reversed(old_views): merged = merge_views(merged, old, known)
            if history[1]: merged = merge_views(merged, history[1]['view'], known)
            if merged != {'groups': view['groups'], 'hidden_ids': view['hidden_ids']}:
                view_store.save_view(**merged, expected_revision=view['revision'], known_ids=known)
            history = without_imported_history(history, {j.job_id for j in store.list_jobs()})
            keys.set_text(HISTORY_KEY, _encoded({'records': history[0], 'context': history[1]}))
            if any(snapshot_digest(Path(product)/name) != value for name, value in before.items()):
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            keys.set_text(MIGRATION_KEY, _encoded({'state': 'partial' if unresolved else 'complete'}))
            return imported
    except Exception:
        keys.set_text(MIGRATION_KEY, _encoded({'state': 'failed'}))
        raise


def migration_entry(arguments, *, automatic=False):
    if arguments: return 2
    from .standalone_paths import frozen_app, service_for_app
    from .standalone_source import uses_local_storage
    app = frozen_app('lulu-agent-core')
    if not uses_local_storage(app): return 2
    # Do not let a killed GUI or rolled-back App leave an import helper alive.
    import threading
    import time
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(.2)
            if os.getppid() != parent: os._exit(1)
    threading.Thread(target=watch_parent, daemon=True).start()
    from .bundle_lifetime import watch_app_replacement
    watch_app_replacement(app, lambda: os._exit(1))
    product = Path.home()/'Library/Application Support/Lulu'
    try:
        from .effects import CurrentUserLaunchctlService
        def reconnect():
            service = CurrentUserLaunchctlService(label=service_for_app(app))
            if service.is_loaded(): service.terminate_registered_process()
        migrate_legacy_tasks(app, product, on_storage_changed=reconnect,
            automatic=automatic, wait_for_idle=True)
        outcome = migration_status(product)
        if outcome not in {'complete', 'partial'}: outcome = 'unchanged'
        print('LULU_MIGRATION_RESULT:'+outcome, flush=True)
        return 0
    except Exception as exc:
        from ..diagnostics import record_failure, failure_code
        record_failure('legacy_task_migration', exc, root=product/'AgentEntry')
        code = getattr(exc, 'reason_code', None) or failure_code(exc)
        print('LULU_MIGRATION_FAILURE:'+(code if code in MIGRATION_FAILURES else 'CORE_OPERATION_FAILED'), flush=True)
        return 1
