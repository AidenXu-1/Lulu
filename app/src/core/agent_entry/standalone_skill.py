"""Optional Skill effects use the existing transaction, outside App readiness."""
import hashlib
import hmac
import os
from pathlib import Path

from .effects import ManagedPathEffect, object_digest
from .installed_materials import _private_json
from .standalone_state import _key, _BINDING_FIELDS
from .ownership import _canonical, verify_ownership_receipt
from .product_install import _write_private
from .transaction import OptionalSkillTransaction, _replace_json


def _targets(installation):
    return {'skill': installation.paths.skill_target, 'launcher': installation.paths.launcher_target}


def _receipt(path, owner, keys):
    value = _private_json(path)
    if (set(value) != {'schema_id', 'manifest', 'digests', 'hmac_sha256'}
            or value['schema_id'] != 'lulu.app-optional-skill/v1'
            or value['manifest'] != owner['component_manifest_sha256']
            or set(value['digests']) != {'skill', 'launcher'}
            or not hmac.compare_digest(value['hmac_sha256'],
                hmac.new(_key(keys, owner['installation_id_digest']), _canonical(value), hashlib.sha256).hexdigest())):
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
    return value


def owned_digests(installation, owner):
    own = installation.paths.state_root/'optional/ownership.json'
    if own.exists():
        return _receipt(own, owner, installation.keys)['digests']
    # Earlier authenticated installation receipts can authorize replacement of
    # precisely their optional files, never a user's unrelated directory.
    root = installation.paths.state_root.parents[1]
    local = getattr(installation.keys, 'storage_scheme', None) == 'local-integrity-v1'
    generations = root/('local-app-generations' if local else 'app-generations')
    if generations.is_symlink():
        raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
    candidates = ([] if local else [root/'ownership.json']) + list(generations.glob('*/ownership.json'))
    actual = {name: object_digest(path) if os.path.lexists(path) else None
              for name, path in _targets(installation).items()}
    if all(value is None for value in actual.values()):
        return actual
    for path in candidates:
        if path.parent.is_symlink():
            raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
        if not path.exists():
            continue
        previous = verify_ownership_receipt(path, key_store=installation.keys)
        if previous['installation_id_digest'] != owner['installation_id_digest']:
            continue
        digests = previous.get('managed_effect_digests', {})
        optional = path.parent/'optional/ownership.json'
        if optional.exists():
            digests = _receipt(optional, previous, installation.keys)['digests']
        if all(digests.get(name) == digest and digest is not None for name, digest in actual.items()):
            return actual
    raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')


def status(installation):
    try:
        owner, _ = installation.verify_installed()
        optional = installation.paths.state_root/'optional'
        if (optional/'current.json').exists() and _private_json(optional/'current.json').get('phase') != 'COMMITTED':
            return 'incomplete'
        digests = owned_digests(installation, owner)
        if all(value is None for value in digests.values()):
            return 'not_installed'
        if not (optional/'ownership.json').exists():
            return 'update_available'
        return 'ready' if all(object_digest(path) == digests[name] for name, path in _targets(installation).items()) else 'ownership_conflict'
    except Exception:
        return 'ownership_conflict'


def install(installation, *, transaction_id, crash_after=None):
    from .product_transaction import product_state_lock
    import shlex
    from .standalone_source import _encoded
    import re
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', transaction_id) is None:
        raise ValueError('invalid transaction id')
    root = installation.paths.state_root/'optional'
    with product_state_lock(root):
        owner, _ = installation.verify_installed()
        if status(installation) == 'ready':
            return {'phase': 'COMMITTED', 'state': 'ready', 'effect_count': 0}
        if (root/'current.json').exists():
            raise RuntimeError('AGENT_ENTRY_RECOVERY_REQUIRED')
        before = owned_digests(installation, owner)
        work = root/'work'/transaction_id
        # Existing portable launcher resolves the current installed App itself.
        launcher = work/'launcher'; _write_private(launcher, ("#!/bin/sh\nset -eu\nexec "+shlex.quote(str(installation.paths.app_bundle/"Contents/MacOS/lulu-agent-cli"))+' "$@"\n').encode()); launcher.chmod(0o700)
        sources = {'skill': installation.paths.app_bundle/'Contents/Resources/skills/lulu-agent', 'launcher': launcher}
        effects = [ManagedPathEffect(name=name, source=sources[name], target=target,
            work_root=work/(name+'-effect'), expected_before_digest=before[name]) for name, target in _targets(installation).items()]
        def final():
            value = {'schema_id': 'lulu.app-optional-skill/v1', 'manifest': owner['component_manifest_sha256'],
                'digests': {name: object_digest(path) for name, path in _targets(installation).items()}}
            value['hmac_sha256'] = hmac.new(_key(installation.keys, owner['installation_id_digest']), _canonical(value), hashlib.sha256).hexdigest()
            _replace_json(root/'ownership.json', value)
            _receipt(root/'ownership.json', owner, installation.keys)
            return True
        transaction = OptionalSkillTransaction(root)
        try:
            return transaction.execute('skill_install', effects, transaction_id=transaction_id,
                binding={name: owner[name] for name in _BINDING_FIELDS}, final_verifier=final, crash_after=crash_after)
        except Exception:
            result = transaction.recover(transaction_id, effects)
            receipt = root/"ownership.json"
            if result.get("state") == "rollback_complete" and receipt.exists():
                _receipt(receipt, owner, installation.keys)
                os.rename(receipt, work/"rolled-back-ownership.json")
            raise
