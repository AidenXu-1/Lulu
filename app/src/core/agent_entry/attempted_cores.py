"""Remember verified candidate code before it can affect historical access.

An OS-approved read can outlive a failed installation. This authenticated list
records code identity only: it is neither a ready receipt nor a task checkpoint.
No Keychain permission is created or changed here.
"""
import hashlib
import hmac
import os
from pathlib import Path

from .installed_materials import _private_json
from .task_handover import _canonical, _key, _validate_source_cores
from ..updater.secure_io import atomic_json

DOMAIN = b'Lulu attempted Core identities v1\0'


def _path(product_root):
    path = Path(product_root)/'AgentEntry/attempted-cores.json'
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    return path


def _binding(product_root, owner):
    return {'schema_id': 'lulu.attempted-core-identities/v1',
            'installation_id': owner['installation_id'],
            'product_root': str(product_root)}


def read_attempted_cores(product_root, owner, keys):
    path = _path(product_root)
    if not os.path.lexists(path):
        return {}
    value = _private_json(path)
    binding = _binding(product_root, owner)
    if (type(value) is not dict or set(value) != {'body', 'hmac_sha256'}
            or type(value['body']) is not dict or set(value['body']) != set(binding) | {'cores'}
            or any(value['body'][name] != expected for name, expected in binding.items())
            or type(value['hmac_sha256']) is not str):
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    expected = hmac.new(_key(keys, owner), DOMAIN+_canonical(value['body']), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, value['hmac_sha256']):
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    return _validate_source_cores(value['body']['cores'])


def remember_attempted_core(proof, *, product_root, owner, keys):
    """Caller holds product_state_lock and supplies inspect_install_source proof.

    Must complete before reading ANY historical private item. If writing fails,
    preparation stops while OS access to those items is still unchanged.
    """
    candidates = [row['sha256'] for row in proof.manifest['components']
                  if row['path'] == 'Contents/MacOS/lulu-agent-core']
    if len(candidates) != 1:
        raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
    pair = _validate_source_cores({candidates[0]: proof.receipt['core_cdhash']})
    known = read_attempted_cores(product_root, owner, keys)
    digest, generation = next(iter(pair.items()))
    if digest in known:
        if known[digest] != generation:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        return pair
    known.update(pair)
    body = _binding(product_root, owner) | {'cores': _validate_source_cores(known)}
    value = {'body': body, 'hmac_sha256': hmac.new(_key(keys, owner),
             DOMAIN+_canonical(body), hashlib.sha256).hexdigest()}
    atomic_json(_path(product_root), value)
    return pair


def current_core_identities(app, *, product_root, generation, keys):
    """Authenticate code allowed to have read this generation's existing items.

    These identities grant no cross-generation write route or OS permission.
    Called once when the measured current Core constructs its business runtime.
    """
    from .standalone_paths import state_for_app
    from .ownership import verify_ownership_receipt, verify_ready_action_receipt
    from .task_handover import FILENAME, open_handover
    from .installed_materials import MATERIALS_DIR
    state = state_for_app(app)
    owner = verify_ownership_receipt(state/'ownership.json', key_store=keys)
    verify_ready_action_receipt(state/'current.json', ownership=owner)
    # Legacy DMG-bound installations have no authenticated Core identity in
    # their owner. Keep their existing strict route; do not infer extra readers.
    if owner.get('source_kind') != 'signed_app':
        return None
    from .standalone_state import app_manifest_path
    app_manifest_path(app, state)
    if owner.get('core_cdhash') != generation or owner.get('app_path') != str(app):
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    value = _private_json(state/MATERIALS_DIR/FILENAME)
    open_handover(value, keys=keys, owner=owner, product_root=product_root)
    known = _validate_source_cores(value['body'].get('source_cores', {}))
    for digest, identity in read_attempted_cores(product_root, owner, keys).items():
        if digest in known and known[digest] != identity:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        known[digest] = identity
    return frozenset({generation, *_validate_source_cores(known).values()})
