"""Authenticated handover captured while the previous installed Core still works.

Uses its public terminal-task projection. No private task key is exported and no
old ledger is changed. An unchanged predecessor can reuse the retained checkpoint; normal startup
reauthenticates history changed by rollback or later work before merging it.
Pre-swap consumers always require the captured snapshot to match exactly.
"""
import hashlib
import hmac
import os
from pathlib import Path
import re

from .task_handover import _canonical, _key, seal_handover, open_handover
from .product_transaction import snapshot_digest
from .installed_materials import _private_json, MATERIALS_DIR
from ..updater.secure_io import atomic_json

DOMAIN = b'Lulu previous Core checkpoint v1\0'
FIELDS = {'schema_id', 'manifest', 'generation', 'snapshot', 'handover', 'state', 'handover_snapshot'}


def checkpoint_path(product_root, owner):
    digest = owner['component_manifest_sha256']
    if type(digest) is not str or re.fullmatch(r'[a-f0-9]{64}', digest) is None:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    path = Path(product_root)/'AgentEntry/history-checkpoints'/(digest+'.json')
    if path.resolve(strict=False) != path:
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    return path


def _task_root(product_root, generation, keys=None):
    if type(generation) is not str or re.fullmatch(r'[a-f0-9]{40}', generation) is None:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    from ..local_integrity import TASK_DIRECTORY
    path = (Path(product_root)/TASK_DIRECTORY if getattr(keys, 'storage_scheme', None) == 'local-integrity-v1'
            else Path(product_root)/'task-generations'/generation)
    if path.resolve(strict=False) != path:
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    return path


def _state_path(product_root, state):
    state = Path(state)
    base = Path(product_root)/'AgentEntry'
    if (state.resolve(strict=False) != state or not (state == base or
            (state.parent in {base/'app-generations', base/'local-app-generations'} and re.fullmatch(r'[a-f0-9]{64}', state.name)))):
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    return state


def write_checkpoint(*, product_root, owner, generation, keys, records, context, expected_snapshot, state, expected_handover_snapshot=None):
    root = _task_root(product_root, generation, keys)
    state = _state_path(product_root, state)
    from .task_handover import FILENAME
    handover = seal_handover(records, keys=keys, owner=owner,
        previous_manifest=owner['component_manifest_sha256'], product_root=product_root,
        gui_context=context)
    if (snapshot_digest(root) != expected_snapshot
            or snapshot_digest(state/MATERIALS_DIR/FILENAME) != expected_handover_snapshot):
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    body = {'schema_id': 'lulu.previous-core-checkpoint/v1',
        'manifest': owner['component_manifest_sha256'], 'generation': generation,
        'snapshot': expected_snapshot, 'handover': handover, 'state': str(state),
        'handover_snapshot': expected_handover_snapshot}
    value = {'body': body, 'hmac_sha256': hmac.new(_key(keys, owner), DOMAIN+_canonical(body), hashlib.sha256).hexdigest()}
    path = checkpoint_path(product_root, owner)
    atomic_json(path, value)
    return path


def read_checkpoint(*, product_root, owner, generation, keys, state, refresh=None):
    path = checkpoint_path(product_root, owner)
    if not os.path.lexists(path):
        return None
    value = _private_json(path)
    if type(value) is not dict or set(value) != {'body', 'hmac_sha256'}:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    body = value['body']
    if (type(body) is not dict or set(body) != FIELDS
            or body['schema_id'] != 'lulu.previous-core-checkpoint/v1'
            or body['manifest'] != owner['component_manifest_sha256']
            or body['generation'] != generation
            or body['state'] != str(_state_path(product_root, state))
            or type(value['hmac_sha256']) is not str):
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    expected = hmac.new(_key(keys, owner), DOMAIN+_canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, value['hmac_sha256']):
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    rows = open_handover(body['handover'], keys=keys, owner=owner, product_root=product_root)
    from .task_handover import FILENAME
    task_root = _task_root(product_root, generation, keys)
    handover_path = Path(state)/MATERIALS_DIR/FILENAME
    before = (snapshot_digest(task_root), snapshot_digest(handover_path))
    cached = rows, body['handover']['body'].get('gui_context')
    if before == (body['snapshot'], body['handover_snapshot']):
        return cached
    # Pre-swap callers remain strict. A later launch may explicitly reauthenticate
    # history changed by a completed rollback or subsequent work in the old App.
    if (refresh is None or (body['snapshot'] is not None and before[0] is None)
            or (body['handover_snapshot'] is not None and before[1] is None)):
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    result = refresh(cached, task_unchanged=before[0] == body['snapshot'])
    if before != (snapshot_digest(task_root), snapshot_digest(handover_path)):
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    return result


def prepare_checkpoint(app, *, keys, run_cli=None):
    """Local installer entry; call before replacing the installed App.

    The caller closes the GUI normally first. Concurrent business changes cause
    rejection here or when the candidate consumes the checkpoint.
    """
    from .standalone_paths import installed_app, state_for_app
    from .standalone_source import core_cdhash
    from .ownership import verify_ownership_receipt, verify_ready_action_receipt
    from .component_manifest import verify_component_manifest
    from .delivery_identity import MANIFEST_NAME
    from .task_handover import canonical_cli, capture_terminal_tasks, FILENAME
    from .task_handover_context import capture_context
    from .standalone_history import merge_history
    app = installed_app(Path(app))
    product = Path.home()/'Library/Application Support/Lulu'
    state = state_for_app(app)
    def verify():
        owner = verify_ownership_receipt(state/'ownership.json', key_store=keys)
        verify_ready_action_receipt(state/'current.json', ownership=owner)
        proof = verify_component_manifest(app, state/MATERIALS_DIR/MANIFEST_NAME)
        if proof['component_manifest_sha256'] != owner['component_manifest_sha256']:
            raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
        return owner
    owner = verify()
    generation = core_cdhash(app)
    before = snapshot_digest(_task_root(product, generation, keys))
    previous = ([], None)
    old_file = state/MATERIALS_DIR/FILENAME
    old_digest = snapshot_digest(old_file)
    if old_file.exists():
        value = _private_json(old_file)
        previous = (open_handover(value, keys=keys, owner=owner, product_root=product),
                    value['body'].get('gui_context'))
    rows = capture_terminal_tasks(run_cli or (lambda args: canonical_cli(args, app=app)),
                                  inherited=previous[0])
    context = capture_context(product, app, rows, previous[1], generation=generation,
        task_root=_task_root(product, generation, keys))
    rows, context = merge_history(previous, (rows, context))
    if verify() != owner or snapshot_digest(old_file) != old_digest or core_cdhash(app) != generation:
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    return write_checkpoint(product_root=product, owner=owner, generation=generation,
        keys=keys, records=rows, context=context, expected_snapshot=before, state=state, expected_handover_snapshot=old_digest)
