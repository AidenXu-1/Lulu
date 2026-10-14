"""Core-only ownership receipt for generation-9 installation and recovery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
from collections.abc import Mapping
from typing import Protocol


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BASE_FIELDS = frozenset({"schema_id", "release_dmg_sha256", "component_manifest_sha256", "app_component_set_digest", "installation_id", "installation_id_digest", "candidate_generation", "hmac_sha256"})
_FIELDS_V1 = _BASE_FIELDS
_FIELDS_V2 = _BASE_FIELDS | {"managed_effect_digests"}
_EFFECT_NAMES = frozenset({"core_registration", "skill", "launcher"})


class OwnershipKeyStore(Protocol):
    def get_text(self, key: str) -> str | None: ...
    def write_new(self, key: str, value: str) -> None: ...
    def delete_text(self, key: str, *, missing_ok: bool = False) -> None: ...


def _key_name(installation_digest: str) -> str:
    return f"lulu_agent_entry_ownership_hmac_key_v1:{installation_digest}"


def _canonical(payload: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "hmac_sha256"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_new(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(path: Path, payload: dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _validated_effect_digests(
    value: Mapping[str, object] | None, *, product_scope: bool = False,
) -> dict[str, str] | None:
    if value is None:
        if product_scope:
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
        return None
    digests = dict(value)
    if (
        frozenset(digests) not in ({_EFFECT_NAMES, frozenset({"core_registration"})} if product_scope else {_EFFECT_NAMES})
        or any(type(item) is not str or not _SHA256.fullmatch(item) for item in digests.values())
    ):
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
    return {name: str(digests[name]) for name in sorted(digests)}


def _payload(
    *,
    release_dmg_sha256: str,
    component_manifest_sha256: str,
    app_component_set_digest: str,
    installation_id: str,
    candidate_generation: int,
    managed_effect_digests: Mapping[str, object] | None,
    product_scope: bool = False,
) -> dict[str, object]:
    if any(not _SHA256.fullmatch(value) for value in (release_dmg_sha256, component_manifest_sha256, app_component_set_digest)) or len(installation_id) < 8 or candidate_generation != 9:
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
    effect_digests = _validated_effect_digests(managed_effect_digests, product_scope=product_scope)
    installation_digest = hashlib.sha256(installation_id.encode()).hexdigest()
    payload: dict[str, object] = {
        "schema_id": (
            "lulu.agent-entry-ownership/v3" if product_scope else "lulu.agent-entry-ownership/v2"
            if effect_digests is not None
            else "lulu.agent-entry-ownership/v1"
        ),
        "release_dmg_sha256": release_dmg_sha256,
        "component_manifest_sha256": component_manifest_sha256,
        "app_component_set_digest": app_component_set_digest,
        "installation_id": installation_id,
        "installation_id_digest": installation_digest,
        "candidate_generation": candidate_generation,
    }
    if effect_digests is not None:
        payload["managed_effect_digests"] = effect_digests
    return payload


def create_ownership_receipt(
    path: Path,
    *,
    key_store: OwnershipKeyStore,
    release_dmg_sha256: str,
    component_manifest_sha256: str,
    app_component_set_digest: str,
    installation_id: str,
    candidate_generation: int,
    managed_effect_digests: Mapping[str, object] | None = None,
    product_scope: bool = False,
) -> dict[str, object]:
    payload = _payload(
        release_dmg_sha256=release_dmg_sha256,
        component_manifest_sha256=component_manifest_sha256,
        app_component_set_digest=app_component_set_digest,
        installation_id=installation_id,
        candidate_generation=candidate_generation,
        managed_effect_digests=managed_effect_digests,
        product_scope=product_scope,
    )
    installation_digest = str(payload["installation_id_digest"])
    key = secrets.token_bytes(32)
    key_name = _key_name(installation_digest)
    key_store.write_new(key_name, base64.b64encode(key).decode("ascii"))
    payload["hmac_sha256"] = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    try:
        _write_new(Path(path), payload)
    except Exception:
        key_store.delete_text(key_name, missing_ok=True)
        raise
    return payload


def replace_ownership_receipt(
    path: Path,
    *,
    key_store: OwnershipKeyStore,
    previous: Mapping[str, object],
    release_dmg_sha256: str,
    component_manifest_sha256: str,
    app_component_set_digest: str,
    candidate_generation: int,
    managed_effect_digests: Mapping[str, object],
    product_scope: bool = False,
) -> dict[str, object]:
    current = verify_ownership_receipt(path, key_store=key_store)
    if current != dict(previous):
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
    payload = _payload(
        release_dmg_sha256=release_dmg_sha256,
        component_manifest_sha256=component_manifest_sha256,
        app_component_set_digest=app_component_set_digest,
        installation_id=str(current["installation_id"]),
        candidate_generation=candidate_generation,
        managed_effect_digests=managed_effect_digests,
        product_scope=product_scope,
    )
    encoded = key_store.get_text(_key_name(str(payload["installation_id_digest"])))
    try:
        key = base64.b64decode(str(encoded), validate=True)
    except Exception as exc:
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID") from exc
    payload["hmac_sha256"] = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    _replace(Path(path), payload)
    return payload


def delete_ownership_materials(
    path: Path,
    *,
    key_store: OwnershipKeyStore,
    expected_binding: dict[str, object],
) -> bool:
    """Delete only ownership material bound to one uncommitted transaction."""

    receipt_path = Path(path)
    installation_digest = expected_binding.get("installation_id_digest")
    if type(installation_digest) is not str or not _SHA256.fullmatch(installation_digest):
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
    key_name = _key_name(installation_digest)
    if os.path.lexists(receipt_path):
        metadata = receipt_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            if type(payload) is not dict or frozenset(payload) not in {_FIELDS_V1, _FIELDS_V2}:
                raise ValueError
            installation_id = payload.get("installation_id")
            if (
                type(installation_id) is not str
                or hashlib.sha256(installation_id.encode()).hexdigest()
                != installation_digest
            ):
                raise ValueError
            expected = {
                "release_dmg_sha256": payload.get("release_dmg_sha256"),
                "component_manifest_sha256": payload.get("component_manifest_sha256"),
                "app_component_set_digest": payload.get("app_component_set_digest"),
                "installation_id_digest": payload.get("installation_id_digest"),
                "candidate_generation": payload.get("candidate_generation"),
            }
            if expected != expected_binding:
                raise ValueError
            encoded_key = key_store.get_text(key_name)
            if encoded_key is not None:
                key = base64.b64decode(encoded_key, validate=True)
                actual_hmac = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(actual_hmac, str(payload.get("hmac_sha256"))):
                    raise ValueError
        except Exception as exc:
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID") from exc
    key_store.delete_text(key_name, missing_ok=True)
    if os.path.lexists(receipt_path):
        receipt_path.unlink()
        descriptor = os.open(receipt_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return not os.path.lexists(receipt_path) and key_store.get_text(key_name) is None


def verify_ownership_receipt(path: Path, *, key_store: OwnershipKeyStore) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if type(payload) is dict and payload.get('schema_id') == 'lulu.agent-entry-ownership/v4':
            from .installed_materials import _private_json
            from .standalone_state import verify_app_owner
            return verify_app_owner(_private_json(Path(path)), key_store)
        fields = frozenset(payload) if type(payload) is dict else frozenset()
        if (
            type(payload) is not dict
            or fields not in {_FIELDS_V1, _FIELDS_V2}
            or payload.get("schema_id")
            not in (
                {"lulu.agent-entry-ownership/v2", "lulu.agent-entry-ownership/v3"}
                if fields == _FIELDS_V2
                else {"lulu.agent-entry-ownership/v1"}
            )
            or payload.get("candidate_generation") != 9
        ):
            raise ValueError
        if fields == _FIELDS_V2:
            _validated_effect_digests(payload.get("managed_effect_digests"), product_scope=payload["schema_id"] == "lulu.agent-entry-ownership/v3")
        installation_id = payload.get("installation_id")
        installation_digest = payload.get("installation_id_digest")
        if type(installation_id) is not str or hashlib.sha256(installation_id.encode()).hexdigest() != installation_digest:
            raise ValueError
        encoded_key = key_store.get_text(_key_name(str(installation_digest)))
        if type(encoded_key) is not str:
            raise ValueError
        key = base64.b64decode(encoded_key, validate=True)
        expected = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(payload.get("hmac_sha256"))):
            raise ValueError
        if any(not _SHA256.fullmatch(str(payload.get(field, ""))) for field in ("release_dmg_sha256", "component_manifest_sha256", "app_component_set_digest", "installation_id_digest", "hmac_sha256")):
            raise ValueError
        return payload
    except Exception as exc:
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID") from exc


def verify_ready_action_receipt(
    path: Path,
    *,
    ownership: dict[str, object],
) -> dict[str, object]:
    from .transaction import validate_installation_binding
    from .product_transaction import assert_business_available
    assert_business_available(Path(path).parent)

    try:
        receipt_path = Path(path)
        metadata = receipt_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError
        descriptor = os.open(
            receipt_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            raw = b""
            while True:
                block = os.read(descriptor, 64 * 1024)
                if not block:
                    break
                raw += block
        finally:
            os.close(descriptor)
        payload = json.loads(raw)
        if type(payload) is dict and payload.get('schema_id') == 'lulu.agent-entry-current-action/v3':
            from .standalone_state import verify_app_action
            return verify_app_action(payload, ownership)
        action = payload.get("action") if type(payload) is dict else None
        effect_count = payload.get("effect_count") if type(payload) is dict else None
        v2 = payload.get("schema_id") == "lulu.agent-entry-current-action/v2" if type(payload) is dict else False
        effect_count_valid = (
            effect_count == 3
            if action == "install"
            else type(effect_count) is int and 0 <= effect_count <= 3
        )
        if v2:
            effect_count_valid = type(effect_count) is int and 0 <= effect_count <= 6
        if (
            type(payload) is not dict
            or frozenset(payload)
            != {
                "schema_id",
                "transaction_id",
                "action",
                "phase",
                "state",
                "effect_count",
                "binding",
            }
            or payload.get("schema_id") not in {"lulu.agent-entry-current-action/v1", "lulu.agent-entry-current-action/v2"}
            or (v2 and ownership.get("schema_id") != "lulu.agent-entry-ownership/v3")
            or (not v2 and ownership.get("schema_id") == "lulu.agent-entry-ownership/v3")
            or action not in ({"product_install", "product_update", "skill_install", "skill_update"} if v2 else {"install", "repair", "entry_update"})
            or payload.get("phase") != "COMMITTED"
            or payload.get("state") != "ready"
            or not effect_count_valid
        ):
            raise ValueError
        binding = validate_installation_binding(payload.get("binding", {}))
        expected = {
            "release_dmg_sha256": ownership.get("release_dmg_sha256"),
            "component_manifest_sha256": ownership.get("component_manifest_sha256"),
            "app_component_set_digest": ownership.get("app_component_set_digest"),
            "installation_id_digest": ownership.get("installation_id_digest"),
            "candidate_generation": ownership.get("candidate_generation"),
        }
        if binding != expected:
            raise ValueError
        return payload
    except Exception as exc:
        raise RuntimeError("AGENT_ENTRY_ACTION_RECEIPT_NOT_READY") from exc
