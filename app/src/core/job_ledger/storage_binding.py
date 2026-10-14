"""Core task storage intent and stable scope. Native secure writer is required.

This is not an installer operation. Caller must hold Core initialization and
product locks after current measured installation and authorization checks.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from datetime import datetime, timezone
from .contracts import integrity_error
from .store import (JobLedgerStore, _LedgerReadProjection, _validate_no_follow_ancestors,
                    _validate_owned_mode, verify_existing_ledger)
from .storage_preflight import inspect_task_storage, authenticate_legacy_scope, _json_object
from ..cli_runtime.keychain import (KeyringJobIntegrityAnchorStore, INSTALLATION_TICKET_KEY,
                                   JOB_HMAC_KEY_PREFIX, JOB_ANCHOR_KEY_PREFIX)
from ..agent_entry.delivery_identity import strict_json
from ..agent_entry.transaction import _fsync_directory

REGISTRY_KEY = "lulu_core_job_registry_v1"
KEY_PREFIX = "lulu_core_job_key_v1:"
ANCHOR_PREFIX = "lulu_core_job_anchor_v1:"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read(path):
    _validate_no_follow_ancestors(path)
    return _LedgerReadProjection()._read_file(path, 64 * 1024)


def _create_exact(path, raw):
    _validate_no_follow_ancestors(path)
    if os.path.lexists(path):
        if _read(path) != raw: raise integrity_error("JOB_BINDING_FILE_CHANGED")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(fd)
    finally: os.close(fd)
    _fsync_directory(path.parent)


def _private_directory(path):
    _validate_no_follow_ancestors(path)
    if not os.path.lexists(path):
        path.mkdir(mode=0o700); _fsync_directory(path.parent)
    _validate_owned_mode(path, mode=0o700, directory=True)


def _cut(selected, point):
    if selected == point: raise RuntimeError("CORE_STORAGE_CRASH:" + point)


def _validate_registry(raw, root, installation_id):
    value = _json_object(raw.encode(), "JOB_REGISTRY_INVALID")
    if (set(value) != {"schema", "phase", "body", "binding_sha256"}
        or value["schema"] != "lulu.core-job-registry/v1" or value["phase"] not in {"INTENT", "READY"}):
        raise integrity_error("JOB_REGISTRY_INVALID")
    body = value["body"]
    if (type(body) is not dict or set(body) != {"scheme", "scope", "root", "installation_id", "operation_id"}
        or body["root"] != str(root) or body["installation_id"] != installation_id
        or body["scheme"] not in {"legacy-v1", "core-v1"}
        or type(body["scope"]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", body["scope"])
        or type(body["operation_id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", body["operation_id"])):
        raise integrity_error("JOB_REGISTRY_BINDING_MISMATCH")
    if body["scheme"] == "core-v1" and body["scope"] != "core-" + installation_id:
        raise integrity_error("JOB_REGISTRY_BINDING_MISMATCH")
    digest = value["binding_sha256"]
    if ((value["phase"] == "INTENT" and digest is not None)
        or (value["phase"] == "READY" and (type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest)))):
        raise integrity_error("JOB_REGISTRY_INVALID")
    return value


class _CoreAnchor:
    def __init__(self, keys, body): self.keys, self.body = keys, body
    def hmac_key(self):
        raw = self.keys.get_text(KEY_PREFIX + self.body["scope"])
        if raw is None: raise integrity_error("JOB_CORE_KEY_MISSING")
        value = _json_object(raw.encode(), "JOB_CORE_KEY_INVALID")
        if set(value) != {"schema", "body", "key"} or value["schema"] != "lulu.core-job-key/v1" or value["body"] != self.body:
            raise integrity_error("JOB_CORE_KEY_BINDING_MISMATCH")
        try: key = base64.b64decode(value["key"], validate=True)
        except Exception as exc: raise integrity_error("JOB_CORE_KEY_INVALID") from exc
        if len(key) != 32: raise integrity_error("JOB_CORE_KEY_INVALID")
        return key
    def load(self):
        raw = self.keys.get_text(ANCHOR_PREFIX + self.body["scope"])
        return None if raw is None else _json_object(raw.encode(), "JOB_HIGH_WATER_INVALID")
    def compare_and_set(self, expected, replacement):
        name = ANCHOR_PREFIX + self.body["scope"]
        raw = self.keys.get_text(name)
        current = None if raw is None else _json_object(raw.encode(), "JOB_HIGH_WATER_INVALID")
        if current != expected: raise integrity_error("JOB_HIGH_WATER_COMPARE_AND_SET_FAILED")
        if raw is None: self.keys.create_core_task_material(name, _json(replacement))
        else: self.keys.replace_core_task_material(name, raw, _json(replacement))


def prepare_core_task_store(product_root: Path, installation_id: str, keys, *, crash_after=None, recovery_at=None, legacy_storage=True):
    """Prepare or recover the exact Core intent without deleting user material."""
    if (not isinstance(product_root, Path) or not product_root.is_absolute()
        or type(installation_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}", installation_id)):
        raise integrity_error("JOB_STORAGE_INSTALLATION_INVALID")
    for method in ("create_core_task_material", "replace_core_task_material"):
        if not callable(getattr(keys, method, None)):
            raise integrity_error("JOB_CORE_SECURE_WRITER_REQUIRED")
    local = getattr(keys, 'storage_scheme', None) == 'local-integrity-v1'
    raw = keys.get_text(REGISTRY_KEY) if local else None
    established_local = local and raw is not None and _validate_registry(raw, product_root, installation_id)['phase'] == 'READY'
    # A committed local binding supplies the scope. Its authenticated pending
    # journal may need recovery before a read-only inventory can be taken.
    inventory = None if established_local else inspect_task_storage(product_root)
    directory = product_root / "task-integrity-v1"
    _validate_no_follow_ancestors(directory)
    if not local: raw = keys.get_text(REGISTRY_KEY)
    if raw is None:
        if os.path.lexists(directory) or inventory.canonical_state not in {"absent", "complete"}:
            raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
        scope = "core-" + installation_id; scheme = "core-v1"
        # A missing registry cannot turn existing deterministic Core material into
        # a new installation or overwrite its high-water mark.
        if any(keys.get_text(prefix + scope) is not None for prefix in (KEY_PREFIX, ANCHOR_PREFIX)):
            raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
        if legacy_storage and inventory.canonical_state == "absent" and (
            keys.get_text(INSTALLATION_TICKET_KEY) is not None
            or any(keys.get_text(prefix + installation_id) is not None
                   for prefix in (JOB_HMAC_KEY_PREFIX, JOB_ANCHOR_KEY_PREFIX))):
            raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
        if inventory.canonical_state == "complete":
            if not legacy_storage:
                raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
            scope, _ = authenticate_legacy_scope(product_root, inventory,
                lambda value: KeyringJobIntegrityAnchorStore(keys, value))
            scheme = "legacy-v1"
        body = {"scheme": scheme, "scope": scope, "root": str(product_root),
            "installation_id": installation_id, "operation_id": secrets.token_hex(16)}
        record = {"schema": "lulu.core-job-registry/v1", "phase": "INTENT", "body": body, "binding_sha256": None}
        raw = _json(record)
        keys.create_core_task_material(REGISTRY_KEY, raw)
        _cut(crash_after, "registered")
    record = _validate_registry(raw, product_root, installation_id)
    body = record["body"]
    intent = (_json(body) + "\n").encode()
    if record["phase"] == "READY":
        _validate_owned_mode(directory, mode=0o700, directory=True)
        if {path.name for path in directory.iterdir()} != {"intent.json", "binding.json"}:
            raise integrity_error("JOB_STORAGE_UNKNOWN_CONTENT")
        if _read(directory / "intent.json") != intent:
            raise integrity_error("JOB_BINDING_FILE_CHANGED")
    else:
        _private_directory(directory)
        names = {path.name for path in directory.iterdir()}
        if names - {"intent.json", "binding.json", "prepared-jobs"}:
            raise integrity_error("JOB_STORAGE_UNKNOWN_CONTENT")
        _create_exact(directory / "intent.json", intent)
        _cut(crash_after, "intent")
    if body["scheme"] == "legacy-v1":
        anchor = KeyringJobIntegrityAnchorStore(keys, body["scope"])
        verify_existing_ledger(product_root / "jobs", anchor)
    else:
        anchor = _CoreAnchor(keys, body)
        account = KEY_PREFIX + body["scope"]
        if keys.get_text(account) is None:
            if (record["phase"] != "INTENT" or inventory.canonical_state != "absent" or anchor.load() is not None
                or os.path.lexists(directory / "binding.json") or os.path.lexists(directory / "prepared-jobs")):
                raise integrity_error("JOB_CORE_KEY_MISSING")
            keys.create_core_task_material(account, _json({"schema": "lulu.core-job-key/v1",
                "body": body, "key": base64.b64encode(secrets.token_bytes(32)).decode()}))
    key = anchor.hmac_key()
    _cut(crash_after, "key")
    binding = (_json({"body": body, "hmac": hmac.new(key, intent, hashlib.sha256).hexdigest()}) + "\n").encode()
    digest = hashlib.sha256(binding).hexdigest()
    if record["phase"] == "READY":
        if record["binding_sha256"] != digest or _read(directory / "binding.json") != binding:
            raise integrity_error("JOB_BINDING_FILE_CHANGED")
    else: _create_exact(directory / "binding.json", binding)
    _cut(crash_after, "binding")
    jobs = product_root / "jobs"
    if body["scheme"] == "core-v1" and not os.path.lexists(jobs):
        if record["phase"] == "READY": raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
        stage = directory / "prepared-jobs"
        if os.path.lexists(stage): verify_existing_ledger(stage, anchor)
        else:
            if anchor.load() is not None: raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
            JobLedgerStore(stage, anchor)
            verify_existing_ledger(stage, anchor)
        if os.path.lexists(jobs): raise integrity_error("JOB_STORAGE_INVENTORY_CHANGED")
        os.rename(stage, jobs); _fsync_directory(directory); _fsync_directory(product_root)
    store = None
    if (getattr(keys, 'storage_scheme', None) == 'local-integrity-v1' and record['phase'] == 'READY'):
        # Resume authenticated pending commits in the established local ledger.
        # Missing files still require recovery; constructor must not recreate
        # an established empty ledger after somebody removed its authority.
        ledger = jobs/'job-ledger-v1'
        _validate_owned_mode(jobs, mode=0o700, directory=True)
        _validate_owned_mode(ledger, mode=0o700, directory=True)
        for name in ('journal.jsonl', 'snapshot.json', 'transaction.lock'):
            _validate_owned_mode(ledger/name, mode=0o600, directory=False)
        store = JobLedgerStore(jobs, anchor, recovery_at=recovery_at or datetime.now(timezone.utc).isoformat())
    verify_existing_ledger(jobs, anchor)
    _cut(crash_after, "ledger")
    if record["phase"] == "INTENT":
        ready = dict(record, phase="READY", binding_sha256=digest)
        keys.replace_core_task_material(REGISTRY_KEY, raw, _json(ready))
    _cut(crash_after, "ready")
    return store or JobLedgerStore(jobs, anchor, recovery_at=recovery_at or datetime.now(timezone.utc).isoformat())
