"""Atomic, independent material generations for an already copied App.

Never replaces a previous generation's receipts or the App itself. An older
helper can restore its App without having to undo this generation's preparation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile

from .component_manifest import verify_component_manifest, _read_regular
from .delivery_identity import MANIFEST_NAME
from .installed_materials import MATERIALS_DIR, _private_json
from .ownership import _canonical, _key_name, verify_ownership_receipt, verify_ready_action_receipt
from .product_install import _write_private
from .product_transaction import product_state_lock, snapshot_digest
from .standalone_source import DESCRIPTOR, _encoded, inspect_install_source
from .task_handover import FILENAME, seal_handover, open_handover
from .transaction import _fsync_directory

OWNER_SCHEMA = 'lulu.agent-entry-ownership/v4'
ACTION_SCHEMA = 'lulu.agent-entry-current-action/v3'
_OWNER_FIELDS = {'schema_id', 'source_kind', 'source_app_sha256', 'descriptor_sha256',
    'component_manifest_sha256', 'app_component_set_digest', 'installation_id',
    'installation_id_digest', 'candidate_generation', 'app_path', 'core_cdhash', 'hmac_sha256'}
_BINDING_FIELDS = ('source_kind', 'source_app_sha256', 'component_manifest_sha256',
                   'app_component_set_digest', 'installation_id_digest', 'candidate_generation')


def install_state_root(app, product_root=None):
    app = Path(app)
    root = Path(product_root) if product_root is not None else Path.home()/'Library/Application Support/Lulu'
    if not app.is_absolute() or app.resolve() != app or root.resolve() != root:
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    descriptor, _ = _read_regular(app/DESCRIPTOR, reason_code='APP_INSTALL_DESCRIPTOR_REQUIRED')
    token = hashlib.sha256(descriptor + b'\0' + os.fsencode(app)).hexdigest()
    from .standalone_source import uses_local_storage
    namespace = 'local-app-generations' if uses_local_storage(app) else 'app-generations'
    return root/'AgentEntry'/namespace/token


def _key(keys, installation_digest):
    try:
        value = base64.b64decode(keys.get_text(_key_name(installation_digest)), validate=True)
        if len(value) != 32:
            raise ValueError
        return value
    except Exception as exc:
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID') from exc


def verify_app_owner(payload, keys):
    if (type(payload) is not dict or set(payload) != _OWNER_FIELDS
            or payload['schema_id'] != OWNER_SCHEMA or payload['source_kind'] != 'signed_app'
            or payload['candidate_generation'] != 9
            or payload['source_app_sha256'] != payload['app_component_set_digest']):
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID')
    for field in ('source_app_sha256', 'descriptor_sha256', 'component_manifest_sha256',
                  'app_component_set_digest', 'installation_id_digest', 'hmac_sha256'):
        if type(payload[field]) is not str or re.fullmatch(r'[a-f0-9]{64}', payload[field]) is None:
            raise RuntimeError('AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID')
    if (type(payload['installation_id']) is not str or len(payload['installation_id']) < 8
            or hashlib.sha256(payload['installation_id'].encode()).hexdigest() != payload['installation_id_digest']
            or type(payload['core_cdhash']) is not str or re.fullmatch(r'[a-f0-9]{40}', payload['core_cdhash']) is None
            or type(payload['app_path']) is not str or not Path(payload['app_path']).is_absolute()):
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID')
    expected = hmac.new(_key(keys, payload['installation_id_digest']), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, payload['hmac_sha256']):
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID')
    return payload


def verify_app_action(payload, owner):
    if (type(payload) is not dict or set(payload) != {'schema_id', 'phase', 'state', 'binding'}
            or payload['schema_id'] != ACTION_SCHEMA or owner.get('schema_id') != OWNER_SCHEMA
            or payload['phase'] != 'COMMITTED' or payload['state'] != 'ready'
            or payload['binding'] != {name: owner[name] for name in _BINDING_FIELDS}):
        raise RuntimeError('AGENT_ENTRY_ACTION_RECEIPT_NOT_READY')
    return payload


def app_manifest_path(app, state_root):
    root = Path(state_root)
    owner = _private_json(root/'ownership.json')
    verify_app_action(_private_json(root/'current.json'), owner)
    descriptor, _ = _read_regular(Path(app)/DESCRIPTOR, reason_code='APP_INSTALL_DESCRIPTOR_REQUIRED')
    if owner.get('app_path') != str(app) or owner.get('descriptor_sha256') != hashlib.sha256(descriptor).hexdigest():
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    source = _private_json(root/MATERIALS_DIR/'verified-app.json')
    expected = {'schema_id': 'lulu.verified-app-install-source/v1', 'source_kind': 'signed_app',
        'descriptor_sha256': owner['descriptor_sha256'], 'component_manifest_sha256': owner['component_manifest_sha256'],
        'app_component_set_digest': owner['app_component_set_digest'], 'candidate_generation': 9,
        'core_cdhash': owner['core_cdhash']}
    path = root/MATERIALS_DIR/MANIFEST_NAME
    from .installed_materials import _private_raw
    if source != expected or hashlib.sha256(_private_raw(path)).hexdigest() != owner['component_manifest_sha256']:
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    return path


def verify_install_state(app, *, state_root, keys):
    root = Path(state_root)
    owner = verify_app_owner(_private_json(root/'ownership.json'), keys)
    path = app_manifest_path(app, root)
    verified = verify_component_manifest(Path(app), path)
    if any(owner[field] != verified[field] for field in ('component_manifest_sha256', 'app_component_set_digest', 'candidate_generation')):
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    from .standalone_runtime import registration_bytes
    from .installed_materials import _private_raw
    if _private_raw(root/'core.plist') != registration_bytes(app, token=install_state_root(app, root.parents[2]).name):
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    return owner, verified


def prepare_install_state(app, *, product_root, keys, previous_state=None, handover_loader=None,
                          verify_existing=True, source_cores=None):
    app, product_root = Path(app), Path(product_root)
    target = install_state_root(app, product_root)
    with product_state_lock(target.parent):
        if os.path.lexists(target):
            if target.is_symlink():
                raise RuntimeError('PRODUCT_PATH_UNSAFE')
            if verify_existing:
                owner, _ = verify_install_state(app, state_root=target, keys=keys)
            else:
                # Runtime's normal GUI/Core handshake verifies the App bytes.
                # Reconcile history without another full bundle scan per launch.
                owner = verify_app_owner(_private_json(target/'ownership.json'), keys)
                app_manifest_path(app, target)
            if previous_state is not None:
                _refresh_history(target, owner, product_root, keys, previous_state, handover_loader, source_cores)
            return target
        before = {} if getattr(keys, 'storage_scheme', None) else {
            name: snapshot_digest(product_root/name) for name in ('jobs', 'task-integrity-v1', 'task-generations')}
        previous = None
        if previous_state is not None:
            previous_state = Path(previous_state)
            if (previous_state.resolve() != previous_state or not (
                    previous_state == product_root/'AgentEntry' or
                    (previous_state.parent == target.parent and re.fullmatch(r'[a-f0-9]{64}', previous_state.name)))):
                raise RuntimeError('PRODUCT_PATH_UNSAFE')
            _private_json(previous_state/'ownership.json')
            previous = verify_ownership_receipt(previous_state/'ownership.json', key_store=keys)
            verify_ready_action_receipt(previous_state/'current.json', ownership=previous)
        elif getattr(keys, 'storage_scheme', None) is None and os.path.lexists(product_root/'AgentEntry/ownership.json'):
            raise RuntimeError('PRODUCT_TASK_HANDOVER_REQUIRED')
        if (previous is None and getattr(keys, 'storage_scheme', None) is None and any(value is not None for value in before.values())) or (previous is not None and handover_loader is None):
            raise RuntimeError('PRODUCT_TASK_HANDOVER_REQUIRED')
        proof = inspect_install_source(app)
        if previous is not None and getattr(keys, 'storage_scheme', None) is None:
            from .attempted_cores import remember_attempted_core
            attempted = remember_attempted_core(proof, product_root=product_root, owner=previous, keys=keys)
            if source_cores is not None:
                if any(digest in source_cores and source_cores[digest] != generation
                       for digest, generation in attempted.items()):
                    raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
                source_cores.update(attempted)
        records, context = handover_loader(previous_state, previous) if previous is not None else ([], None)
        installation_id = previous['installation_id'] if previous else getattr(keys, 'installation_id', None) or 'app-'+secrets.token_hex(32)
        installation_digest = hashlib.sha256(installation_id.encode()).hexdigest()
        created_key = False
        stage = Path(tempfile.mkdtemp(prefix='.preparing-', dir=target.parent))
        try:
            if previous is None:
                name = _key_name(installation_digest)
                if getattr(keys, 'storage_scheme', None) is None or keys.get_text(name) is None:
                    keys.write_new(name, base64.b64encode(secrets.token_bytes(32)).decode())
                    created_key = True
            owner = {'schema_id': OWNER_SCHEMA, 'source_kind': 'signed_app',
                'source_app_sha256': proof.receipt['app_component_set_digest'],
                'descriptor_sha256': proof.receipt['descriptor_sha256'],
                'component_manifest_sha256': proof.receipt['component_manifest_sha256'],
                'app_component_set_digest': proof.receipt['app_component_set_digest'],
                'installation_id': installation_id, 'installation_id_digest': installation_digest,
                'candidate_generation': 9, 'app_path': str(app), 'core_cdhash': proof.receipt['core_cdhash']}
            owner['hmac_sha256'] = hmac.new(_key(keys, installation_digest), _canonical(owner), hashlib.sha256).hexdigest()
            handover = seal_handover(records, keys=keys, owner=owner, product_root=product_root,
                previous_manifest=previous['component_manifest_sha256'] if previous else None, gui_context=context,
                source_cores=source_cores)
            _write_private(stage/MATERIALS_DIR/MANIFEST_NAME, proof.manifest_bytes)
            _write_private(stage/MATERIALS_DIR/'verified-app.json', _encoded(proof.receipt))
            _write_private(stage/MATERIALS_DIR/FILENAME, _encoded(handover))
            _write_private(stage/'ownership.json', _encoded(owner))
            from .standalone_runtime import registration_bytes
            _write_private(stage/'core.plist', registration_bytes(app, token=target.name))
            _write_private(stage/'current.json', _encoded({'schema_id': ACTION_SCHEMA, 'phase': 'COMMITTED',
                'state': 'ready', 'binding': {name: owner[name] for name in _BINDING_FIELDS}}))
            verify_install_state(app, state_root=stage, keys=keys)
            open_handover(_private_json(stage/MATERIALS_DIR/FILENAME), keys=keys, owner=owner, product_root=product_root)
            if any(snapshot_digest(product_root/name) != value for name, value in before.items()):
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            if previous is not None and verify_ownership_receipt(previous_state/'ownership.json', key_store=keys) != previous:
                raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
            os.rename(stage, target)
            _fsync_directory(target.parent)
            return target
        except Exception:
            if created_key and not target.exists() and getattr(keys, 'storage_scheme', None) is None:
                keys.delete_text(_key_name(installation_digest), missing_ok=True)
            # Preserve only this attempt's incomplete metadata for diagnosis.
            # No old generation or user content is changed or removed.
            raise


def _refresh_history(target, owner, product_root, keys, previous_state, loader, source_cores):
    """Retry a prepared candidate without freezing the predecessor's old snapshot."""
    previous_state = Path(previous_state)
    if (previous_state == target or previous_state.resolve() != previous_state or not (
            previous_state == product_root/'AgentEntry' or
            previous_state.parent == target.parent and re.fullmatch(r'[a-f0-9]{64}', previous_state.name))):
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    previous = verify_ownership_receipt(previous_state/'ownership.json', key_store=keys)
    verify_ready_action_receipt(previous_state/'current.json', ownership=previous)
    if previous['installation_id'] != owner['installation_id'] or loader is None:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    before = _historical_snapshot(product_root, owner['core_cdhash'])
    path = target/MATERIALS_DIR/FILENAME
    current = _private_json(path)
    records = open_handover(current, keys=keys, owner=owner, product_root=product_root)
    from .standalone_history import merge_history
    records, context = merge_history((records, current['body'].get('gui_context')), loader(previous_state, previous))
    refreshed = seal_handover(records, keys=keys, owner=owner, product_root=product_root,
        previous_manifest=previous['component_manifest_sha256'], gui_context=context,
        source_cores={**current['body'].get('source_cores', {}), **(source_cores or {})})
    if _historical_snapshot(product_root, owner['core_cdhash']) != before:
        raise RuntimeError('PRODUCT_TASKS_CHANGED')
    if verify_ownership_receipt(previous_state/'ownership.json', key_store=keys) != previous:
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
    if refreshed == current:
        return
    # Only replace this candidate's authenticated history after every check.
    temporary = path.with_name('.handover-'+secrets.token_hex(16)+'.json')
    _write_private(temporary, _encoded(refreshed))
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _historical_snapshot(root, current_generation):
    # Reopening this GUI may reconnect to its own still-running Core tasks.
    # Only predecessor ledgers must stay fixed while their history is read.
    result = {name: snapshot_digest(root/name) for name in ('jobs', 'task-integrity-v1')}
    generations = root/'task-generations'
    if generations.exists():
        if generations.is_symlink():
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        for path in generations.iterdir():
            if path.name != current_generation:
                result['task-generations/'+path.name] = snapshot_digest(path)
    return result
