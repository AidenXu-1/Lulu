"""Locate v2 material only through a committed, DMG-bound installed product receipt."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
from .delivery_identity import DELIVERY_MARKER, MANIFEST_NAME, DMG_SCHEMA, LINK_POLICY, strict_json, verify_delivery_marker

MATERIALS_DIR='product-materials-v2'


def _private_raw(path):
    from .delivery_package import _read
    path=Path(path)
    raw,meta=_read(path)
    if meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)!=0o600:
        raise RuntimeError('AGENT_ENTRY_RECEIPT_REQUIRED')
    return raw


def _private_json(path):
    return strict_json(_private_raw(path))


def component_manifest_path(app: Path, *, state_root: Path | None = None) -> Path:
    app=Path(app)
    from .standalone_source import DESCRIPTOR
    if os.path.lexists(app/DESCRIPTOR):
        from .standalone_state import app_manifest_path, install_state_root
        return app_manifest_path(app, Path(state_root) if state_root is not None else install_state_root(app))
    if not os.path.lexists(app/DELIVERY_MARKER) and not os.path.lexists(app/LINK_POLICY) and (app/"Contents/Resources/agent_product_manifest.json").is_file():
        return app/'Contents/Resources/agent_product_manifest.json'
    verify_delivery_marker(app)
    state=Path(state_root) if state_root is not None else Path.home()/'Library/Application Support/Lulu/AgentEntry'
    from .product_transaction import assert_business_available
    assert_business_available(state)
    current=_private_json(state/'current.json')
    if current.get('schema_id')!='lulu.agent-entry-current-action/v2' or current.get('phase')!='COMMITTED' or current.get('state')!='ready':
        raise RuntimeError('AGENT_ENTRY_RECEIPT_REQUIRED')
    receipt=_private_json(state/MATERIALS_DIR/'verified-dmg.json')
    binding=current.get('binding',{})
    if receipt.get('schema_id')!=DMG_SCHEMA or receipt.get('verified') is not True or receipt.get('detached') is not True or receipt.get('mounted_readonly') is not True:
        raise RuntimeError('AGENT_ENTRY_RECEIPT_REQUIRED')
    for field in ('release_dmg_sha256','component_manifest_sha256','app_component_set_digest','candidate_generation'):
        if binding.get(field)!=receipt.get(field):raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    path=state/MATERIALS_DIR/MANIFEST_NAME
    raw=_private_raw(path)
    manifest=strict_json(raw)
    if hashlib.sha256(raw).hexdigest()!=binding.get('component_manifest_sha256') or manifest.get('app_component_set_digest')!=binding.get('app_component_set_digest'):
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    return path
