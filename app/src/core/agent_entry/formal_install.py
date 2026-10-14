"""Formal product installer for the measured generation-9 Agent entry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Protocol

from .canonical_app_tree import CanonicalAppTreeError, canonical_app_tree_identity
from .component_manifest import verify_component_manifest
from .effects import (
    CoreRegistrationEffect,
    ManagedPathEffect,
    ServiceController,
    object_digest,
)
from .ownership import (
    OwnershipKeyStore,
    create_ownership_receipt,
    delete_ownership_materials,
    replace_ownership_receipt,
    verify_ownership_receipt,
    verify_ready_action_receipt,
)
from .transaction import AgentEntryTransaction, TransactionError


DMG_RECEIPT_SCHEMA = "lulu.generation9-dmg-verification/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,159}$")
_DMG_RECEIPT_FIELDS = frozenset(
    {
        "schema_id",
        "verified",
        "release_dmg_sha256",
        "release_dmg_size",
        "release_dmg_identity",
        "expected_app",
        "app_component_set_digest",
        "component_manifest_sha256",
        "product_version",
        "product_build",
        "source_revision",
        "candidate_generation",
        "mounted_readonly",
        "detached",
        "contains_secret_material",
    }
)
_DMG_IDENTITY_FIELDS = frozenset(
    {"path", "device", "inode", "mode", "owner", "size", "mtime_ns"}
)
_DMG_APP_BINDING_FIELDS = (
    "app_component_set_digest",
    "component_manifest_sha256",
    "product_version",
    "product_build",
    "source_revision",
    "candidate_generation",
)
LAUNCHER_BYTES = (
    b"#!/bin/sh\n"
    b"# lulu.agent-launcher/v1\n"
    b"set -eu\n"
    b'exec /Applications/Lulu.app/Contents/MacOS/lulu-agent-cli "$@"\n'
)


class DmgSelectionRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PACKAGE_DMG_SELECTION_REQUIRED")
        self.reason_code = "PACKAGE_DMG_SELECTION_REQUIRED"
        self.state = "dmg_selection_required"
        self.effect_count = 0


class InstallationIdFactory(Protocol):
    def __call__(self) -> str: ...


@dataclass(frozen=True, slots=True)
class FormalInstallPaths:
    app_bundle: Path
    dmg: Path
    dmg_receipt: Path
    state_root: Path
    launch_agent_target: Path
    skill_target: Path
    launcher_target: Path

    def __post_init__(self) -> None:
        for value in (
            self.app_bundle,
            self.dmg,
            self.dmg_receipt,
            self.state_root,
            self.launch_agent_target,
            self.skill_target,
            self.launcher_target,
        ):
            if not Path(value).is_absolute():
                raise ValueError("formal install paths must be absolute")


def _read_regular(path: Path, *, private: bool = False) -> tuple[bytes, os.stat_result]:
    before = Path(path).lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or (private and (stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.getuid()))
    ):
        raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        current = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
        digest = bytearray()
        while block := os.read(descriptor, 1024 * 1024):
            digest.extend(block)
        return bytes(digest), current
    finally:
        os.close(descriptor)


def _load_dmg_receipt(path: Path) -> dict[str, object]:
    try:
        raw, _metadata = _read_regular(path, private=True)
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("PACKAGE_VERIFICATION_RECEIPT_INVALID") from exc
    identity = payload.get("release_dmg_identity") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or frozenset(payload) != _DMG_RECEIPT_FIELDS
        or payload.get("schema_id") != DMG_RECEIPT_SCHEMA
        or payload.get("verified") is not True
        or payload.get("mounted_readonly") is not True
        or payload.get("detached") is not True
        or payload.get("contains_secret_material") is not False
        or payload.get("candidate_generation") != 9
        or type(identity) is not dict
        or frozenset(identity) != _DMG_IDENTITY_FIELDS
        or any(
            not _SHA256.fullmatch(str(payload.get(field, "")))
            for field in (
                "release_dmg_sha256",
                "app_component_set_digest",
                "component_manifest_sha256",
            )
        )
    ):
        raise RuntimeError("PACKAGE_VERIFICATION_RECEIPT_INVALID")
    return payload


def _verify_dmg_binding(dmg: Path, receipt: dict[str, object]) -> str:
    try:
        raw, metadata = _read_regular(dmg)
        identity = receipt["release_dmg_identity"]
        actual_identity = {
            "path": str(dmg),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "owner": metadata.st_uid,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        digest = hashlib.sha256(raw).hexdigest()
        if (
            identity != actual_identity
            or receipt.get("release_dmg_size") != len(raw)
            or receipt.get("release_dmg_sha256") != digest
        ):
            raise RuntimeError("PACKAGE_HASH_MISMATCH")
        return digest
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("PACKAGE_HASH_MISMATCH") from exc


def _run_system(arguments: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _mounted_device(raw: bytes, mountpoint: Path) -> str:
    try:
        payload = plistlib.loads(raw)
        entities = payload.get("system-entities") if type(payload) is dict else None
        matches = [
            item
            for item in entities or []
            if type(item) is dict
            and type(item.get("mount-point")) is str
            and Path(str(item["mount-point"])).resolve() == mountpoint.resolve()
        ]
        if len(matches) != 1 or type(matches[0].get("dev-entry")) is not str:
            raise ValueError
        return str(matches[0]["dev-entry"])
    except Exception as exc:
        raise RuntimeError("PACKAGE_MOUNT_FAILED") from exc


def _mount_is_readonly(mountpoint: Path) -> bool:
    result = _run_system(["/sbin/mount"], timeout=30)
    marker = f" on {mountpoint.resolve()} (".encode()
    return result.returncode == 0 and any(
        marker in line and b"read-only" in line for line in result.stdout.splitlines()
    )


def _trusted_mounted_app(mountpoint: Path, candidate: Path) -> Path:
    """Return a real App directory whose resolved root remains inside this mount."""

    try:
        mount_root = mountpoint.resolve(strict=True)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(mount_root)
        if relative == Path("."):
            raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
        return candidate
    except RuntimeError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH") from exc


def _manifest_identity(app: Path) -> tuple[dict[str, object], str, bytes]:
    manifest_path = app / "Contents/Resources/agent_product_manifest.json"
    raw, _metadata = _read_regular(manifest_path)
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH") from exc
    required = {
        "schema_id": "lulu.agent-component-manifest/v1",
        "candidate_generation": 9,
        "contains_secret_material": False,
    }
    if (
        type(payload) is not dict
        or any(payload.get(key) != value for key, value in required.items())
        or any(
            type(payload.get(field)) is not str or not payload.get(field)
            for field in (
                "product_version",
                "product_build",
                "source_revision",
                "app_component_set_digest",
            )
        )
        or not _SHA256.fullmatch(str(payload.get("app_component_set_digest", "")))
    ):
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
    try:
        tree = canonical_app_tree_identity(app)
    except Exception as exc:
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH") from exc
    if tree["sha256"] != payload["app_component_set_digest"]:
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
    return payload, hashlib.sha256(raw).hexdigest(), raw


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700, follow_symlinks=False)
    if os.path.lexists(target):
        _read_regular(target, private=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        os.chmod(target, 0o600, follow_symlinks=False)
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def provision_verified_dmg_receipt(
    dmg: Path,
    *,
    expected_app: Path,
    receipt_path: Path,
) -> dict[str, object]:
    """Verify a user-selected DMG against the installed App before writing state."""

    dmg = Path(dmg)
    expected_app = Path(expected_app)
    receipt_path = Path(receipt_path)
    if (
        not dmg.is_absolute()
        or dmg.suffix.lower() != ".dmg"
        or not expected_app.is_absolute()
        or expected_app.name != "Lulu.app"
        or not receipt_path.is_absolute()
    ):
        raise RuntimeError("INVALID_ARGUMENT")
    raw, metadata = _read_regular(dmg)
    dmg_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        expected_tree = canonical_app_tree_identity(expected_app)
    except CanonicalAppTreeError as exc:
        raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH") from exc
    expected_manifest, expected_manifest_sha256, expected_manifest_raw = _manifest_identity(
        expected_app
    )
    verify = _run_system(["/usr/bin/hdiutil", "verify", str(dmg)], timeout=300)
    if verify.returncode != 0:
        raise RuntimeError("PACKAGE_HASH_MISMATCH")
    device: str | None = None
    detached = False
    with TemporaryDirectory(prefix="lulu-generation9-user-mount-") as temporary_root:
        mountpoint = Path(temporary_root)
        attach = _run_system(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-plist",
                "-mountpoint",
                str(mountpoint),
                str(dmg),
            ],
            timeout=120,
        )
        if attach.returncode != 0:
            raise RuntimeError("PACKAGE_MOUNT_FAILED")
        try:
            device = _mounted_device(attach.stdout, mountpoint)
            if not _mount_is_readonly(mountpoint):
                raise RuntimeError("PACKAGE_MOUNT_NOT_READONLY")
            root_apps = [path for path in mountpoint.iterdir() if path.name == "Lulu.app"]
            all_apps = [path for path in mountpoint.rglob("*.app")]
            if len(root_apps) != 1 or len(all_apps) != 1:
                raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
            mounted_app = _trusted_mounted_app(mountpoint, root_apps[0])
            try:
                mounted_tree = canonical_app_tree_identity(mounted_app)
            except CanonicalAppTreeError as exc:
                raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH") from exc
            mounted_manifest, mounted_manifest_sha256, mounted_manifest_raw = _manifest_identity(
                mounted_app
            )
            if (
                mounted_tree != expected_tree
                or mounted_manifest != expected_manifest
                or mounted_manifest_raw != expected_manifest_raw
                or mounted_manifest_sha256 != expected_manifest_sha256
            ):
                raise RuntimeError("PACKAGE_PAYLOAD_MISMATCH")
        finally:
            detach = _run_system(
                ["/usr/bin/hdiutil", "detach", device or str(mountpoint)], timeout=120
            )
            detached = detach.returncode == 0
        if not detached or mountpoint.is_mount():
            raise RuntimeError("PACKAGE_DETACH_FAILED")
    payload: dict[str, object] = {
        "schema_id": DMG_RECEIPT_SCHEMA,
        "verified": True,
        "release_dmg_sha256": dmg_sha256,
        "release_dmg_size": len(raw),
        "release_dmg_identity": {
            "path": str(dmg),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "owner": metadata.st_uid,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        },
        "expected_app": str(expected_app),
        "app_component_set_digest": expected_tree["sha256"],
        "component_manifest_sha256": expected_manifest_sha256,
        "product_version": expected_manifest["product_version"],
        "product_build": expected_manifest["product_build"],
        "source_revision": expected_manifest["source_revision"],
        "candidate_generation": 9,
        "mounted_readonly": True,
        "detached": True,
        "contains_secret_material": False,
    }
    _write_private_json(receipt_path, payload)
    return payload


class FormalAgentEntryInstaller:
    def __init__(
        self,
        paths: FormalInstallPaths,
        *,
        key_store: OwnershipKeyStore,
        service: ServiceController,
        installation_id_factory: Callable[[], str],
    ) -> None:
        self.paths = paths
        self._keys = key_store
        self._service = service
        self._installation_id_factory = installation_id_factory

    def _prepare_launcher_source(self) -> Path:
        root = self.paths.state_root / "materials"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        source = root / "lulu"
        if source.exists():
            raw, metadata = _read_regular(source)
            if raw != LAUNCHER_BYTES or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
            return source
        descriptor = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700,
        )
        try:
            os.write(descriptor, LAUNCHER_BYTES)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return source

    def _effects(
        self,
        expected_before_digests: dict[str, str] | None = None,
    ) -> list[object]:
        app = self.paths.app_bundle
        launcher_source = self._prepare_launcher_source()
        previous = expected_before_digests or {}
        return [
            CoreRegistrationEffect(
                source=app / "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
                target=self.paths.launch_agent_target,
                work_root=self.paths.state_root / "effects/core_registration",
                service=self._service,
                expected_before_digest=previous.get("core_registration"),
            ),
            ManagedPathEffect(
                name="skill",
                source=app / "Contents/Resources/skills/lulu-agent",
                target=self.paths.skill_target,
                work_root=self.paths.state_root / "effects/skill",
                expected_before_digest=previous.get("skill"),
            ),
            ManagedPathEffect(
                name="launcher",
                source=launcher_source,
                target=self.paths.launcher_target,
                work_root=self.paths.state_root / "effects/launcher",
                expected_before_digest=previous.get("launcher"),
            ),
        ]

    def _update_backup_path(self, transaction_id: str) -> Path:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise ValueError("invalid transaction id")
        return self.paths.state_root / "update-backups" / f"{transaction_id}.json"

    @staticmethod
    def _remove_private_file(path: Path) -> None:
        if not os.path.lexists(path):
            return
        _read_regular(path, private=True)
        path.unlink()
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def status(self) -> str:
        """Read the installed Skill relationship without changing any managed path."""

        ownership_path = self.paths.state_root / "ownership.json"
        current_path = self.paths.state_root / "current.json"
        targets = {
            "core_registration": self.paths.launch_agent_target,
            "skill": self.paths.skill_target,
            "launcher": self.paths.launcher_target,
        }
        ownership_exists = os.path.lexists(ownership_path)
        current_exists = os.path.lexists(current_path)
        if not ownership_exists and not current_exists:
            if self._service.is_loaded() or any(
                os.path.lexists(path) for path in targets.values()
            ):
                return "ownership_conflict"
            return "not_installed"
        if ownership_exists != current_exists:
            return "incomplete"
        try:
            verified = verify_component_manifest(
                self.paths.app_bundle,
                self.paths.app_bundle
                / "Contents/Resources/agent_product_manifest.json",
            )
            ownership = verify_ownership_receipt(
                ownership_path,
                key_store=self._keys,
            )
            current = verify_ready_action_receipt(
                current_path,
                ownership=ownership,
            )
            if not self._service.is_loaded():
                return "incomplete"
            target_digests = {
                name: object_digest(path) for name, path in targets.items()
            }
            stored = ownership.get("managed_effect_digests")
            previous_digests = (
                {name: str(stored[name]) for name in targets}
                if type(stored) is dict
                else target_digests
            )
            launcher_digest = hashlib.sha256(
                b"file\0" + b"0700" + b"\0" + LAUNCHER_BYTES
            ).hexdigest()
            bundled_digests = {
                "core_registration": object_digest(
                    self.paths.app_bundle
                    / "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist"
                ),
                "skill": object_digest(
                    self.paths.app_bundle
                    / "Contents/Resources/skills/lulu-agent"
                ),
                "launcher": launcher_digest,
            }
            expected_binding = {
                "release_dmg_sha256": ownership["release_dmg_sha256"],
                "component_manifest_sha256": verified["component_manifest_sha256"],
                "app_component_set_digest": verified["app_component_set_digest"],
                "installation_id_digest": ownership["installation_id_digest"],
                "candidate_generation": 9,
            }
            if (
                target_digests == bundled_digests
                and current.get("binding") == expected_binding
            ):
                return "ready"
            if target_digests == previous_digests:
                return "update_available"
            return "ownership_conflict"
        except Exception:
            return "incompatible"

    def install(
        self,
        *,
        transaction_id: str,
        crash_after: str | None = None,
    ) -> dict[str, object]:
        receipt = _load_dmg_receipt(self.paths.dmg_receipt)
        release_sha256 = _verify_dmg_binding(self.paths.dmg, receipt)
        verified = verify_component_manifest(
            self.paths.app_bundle,
            self.paths.app_bundle / "Contents/Resources/agent_product_manifest.json",
        )
        for field in _DMG_APP_BINDING_FIELDS:
            if receipt.get(field) != verified.get(field):
                raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")

        release_sha256 = str(release_sha256)
        ownership_path = self.paths.state_root / "ownership.json"
        current_path = self.paths.state_root / "current.json"
        previous_ownership: dict[str, object] | None = None
        previous_current: dict[str, object] | None = None
        previous_digests: dict[str, str] | None = None
        if os.path.lexists(ownership_path) or os.path.lexists(current_path):
            try:
                previous_ownership = verify_ownership_receipt(
                    ownership_path,
                    key_store=self._keys,
                )
                previous_current = verify_ready_action_receipt(
                    current_path,
                    ownership=previous_ownership,
                )
                stored = previous_ownership.get("managed_effect_digests")
                if type(stored) is dict:
                    previous_digests = {
                        name: str(stored[name])
                        for name in ("core_registration", "skill", "launcher")
                    }
                else:
                    previous_digests = {
                        "core_registration": object_digest(
                            self.paths.launch_agent_target
                        ),
                        "skill": object_digest(self.paths.skill_target),
                        "launcher": object_digest(self.paths.launcher_target),
                    }
            except Exception as exc:
                raise TransactionError(
                    "AGENT_ENTRY_OWNERSHIP_CONFLICT",
                    "ownership_conflict",
                    effect_count=0,
                ) from exc

        effects = self._effects(previous_digests)
        if all(effect.probe().state == "after" for effect in effects):
            try:
                if previous_ownership is None or previous_current is None:
                    raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
                expected = {
                    "release_dmg_sha256": release_sha256,
                    "component_manifest_sha256": str(
                        verified["component_manifest_sha256"]
                    ),
                    "app_component_set_digest": str(
                        verified["app_component_set_digest"]
                    ),
                    "installation_id_digest": previous_ownership[
                        "installation_id_digest"
                    ],
                    "candidate_generation": 9,
                }
                if previous_current.get("binding") != expected:
                    raise RuntimeError("AGENT_ENTRY_VERSION_MISMATCH")
            except Exception as exc:
                raise TransactionError(
                    "AGENT_ENTRY_OWNERSHIP_CONFLICT",
                    "ownership_conflict",
                    effect_count=0,
                ) from exc
            result = dict(previous_current)
            result["effect_count"] = 0
            return result

        installation_id = (
            str(previous_ownership["installation_id"])
            if previous_ownership is not None
            else self._installation_id_factory()
        )
        if type(installation_id) is not str or len(installation_id) < 8:
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
        installation_id_digest = hashlib.sha256(installation_id.encode()).hexdigest()
        binding = {
            "release_dmg_sha256": release_sha256,
            "component_manifest_sha256": str(verified["component_manifest_sha256"]),
            "app_component_set_digest": str(verified["app_component_set_digest"]),
            "installation_id_digest": installation_id_digest,
            "candidate_generation": 9,
        }
        action = "entry_update" if previous_ownership is not None else "install"
        update_backup: Path | None = None
        if previous_ownership is not None and previous_current is not None:
            update_backup = self._update_backup_path(transaction_id)
            if os.path.lexists(update_backup):
                raise RuntimeError("AGENT_ENTRY_RECOVERY_REQUIRED")
            _write_private_json(
                update_backup,
                {
                    "schema_id": "lulu.agent-skill-update-backup/v1",
                    "ownership": previous_ownership,
                    "current": previous_current,
                    "managed_effect_digests": previous_digests,
                },
            )

        def final_verifier() -> bool:
            if any(effect.probe().state != "after" for effect in effects):
                return False
            managed_effect_digests = {
                "core_registration": object_digest(self.paths.launch_agent_target),
                "skill": object_digest(self.paths.skill_target),
                "launcher": object_digest(self.paths.launcher_target),
            }
            if previous_ownership is None:
                ownership = create_ownership_receipt(
                    ownership_path,
                    key_store=self._keys,
                    release_dmg_sha256=release_sha256,
                    component_manifest_sha256=str(verified["component_manifest_sha256"]),
                    app_component_set_digest=str(verified["app_component_set_digest"]),
                    installation_id=installation_id,
                    candidate_generation=9,
                    managed_effect_digests=managed_effect_digests,
                )
            else:
                ownership = replace_ownership_receipt(
                    ownership_path,
                    key_store=self._keys,
                    previous=previous_ownership,
                    release_dmg_sha256=release_sha256,
                    component_manifest_sha256=str(verified["component_manifest_sha256"]),
                    app_component_set_digest=str(verified["app_component_set_digest"]),
                    candidate_generation=9,
                    managed_effect_digests=managed_effect_digests,
                )
            return verify_ownership_receipt(ownership_path, key_store=self._keys) == ownership

        result = AgentEntryTransaction(self.paths.state_root).execute(
            action,
            effects,
            transaction_id=transaction_id,
            binding=binding,
            final_verifier=final_verifier,
            crash_after=crash_after,
        )
        ownership = verify_ownership_receipt(ownership_path, key_store=self._keys)
        verify_ready_action_receipt(self.paths.state_root / "current.json", ownership=ownership)
        if update_backup is not None:
            self._remove_private_file(update_backup)
        return result

    def recover(self, *, transaction_id: str) -> dict[str, object]:
        backup_path = self._update_backup_path(transaction_id)
        backup: dict[str, object] | None = None
        expected_before: dict[str, str] | None = None
        if os.path.lexists(backup_path):
            try:
                raw, _metadata = _read_regular(backup_path, private=True)
                loaded = json.loads(raw)
                if (
                    type(loaded) is not dict
                    or frozenset(loaded)
                    != {
                        "schema_id",
                        "ownership",
                        "current",
                        "managed_effect_digests",
                    }
                    or loaded.get("schema_id")
                    != "lulu.agent-skill-update-backup/v1"
                    or type(loaded.get("ownership")) is not dict
                    or type(loaded.get("current")) is not dict
                    or type(loaded.get("managed_effect_digests")) is not dict
                ):
                    raise ValueError
                expected_before = {
                    name: str(loaded["managed_effect_digests"][name])
                    for name in ("core_registration", "skill", "launcher")
                }
                if any(not _SHA256.fullmatch(value) for value in expected_before.values()):
                    raise ValueError
                backup = loaded
            except Exception as exc:
                raise RuntimeError("AGENT_ENTRY_RECOVERY_REQUIRED") from exc

        def restore_ownership(binding: dict[str, object]) -> bool:
            ownership_path = self.paths.state_root / "ownership.json"
            if backup is None:
                return delete_ownership_materials(
                    ownership_path,
                    key_store=self._keys,
                    expected_binding=binding,
                )
            previous = dict(backup["ownership"])
            previous_raw = (
                json.dumps(previous, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            try:
                current_raw, _metadata = _read_regular(ownership_path, private=True)
                if current_raw != previous_raw:
                    current = verify_ownership_receipt(
                        ownership_path,
                        key_store=self._keys,
                    )
                    current_binding = {
                        "release_dmg_sha256": current.get("release_dmg_sha256"),
                        "component_manifest_sha256": current.get(
                            "component_manifest_sha256"
                        ),
                        "app_component_set_digest": current.get(
                            "app_component_set_digest"
                        ),
                        "installation_id_digest": current.get(
                            "installation_id_digest"
                        ),
                        "candidate_generation": current.get(
                            "candidate_generation"
                        ),
                    }
                    if current_binding != binding:
                        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID")
                    _write_private_json(ownership_path, previous)
                return (
                    verify_ownership_receipt(
                        ownership_path,
                        key_store=self._keys,
                    )
                    == previous
                )
            except Exception:
                return False

        result = AgentEntryTransaction(self.paths.state_root).recover(
            transaction_id,
            self._effects(expected_before),
            rollback_verifier=restore_ownership,
        )
        if backup is not None and result.get("state") == "rollback_complete":
            current_path = self.paths.state_root / "current.json"
            _write_private_json(current_path, dict(backup["current"]))
            ownership = verify_ownership_receipt(
                self.paths.state_root / "ownership.json",
                key_store=self._keys,
            )
            verify_ready_action_receipt(current_path, ownership=ownership)
            self._remove_private_file(backup_path)
        return result


def build_default_formal_installer(dmg: Path | None = None) -> FormalAgentEntryInstaller:
    """Build the installed-App entry, provisioning proof from a selected DMG if needed."""

    from ..license.store import StrictKeyringStore
    from .effects import CurrentUserLaunchctlService

    from .standalone_source import DESCRIPTOR
    import sys
    frozen_app = Path(sys.executable).parent.parent.parent
    if getattr(sys, "frozen", False) and (frozen_app/DESCRIPTOR).exists():
        app = frozen_app
    else:
        app = Path("/Applications/Lulu.app")
    if os.path.lexists(app/DESCRIPTOR) or os.path.lexists(app/"Contents/Resources/component_delivery.v2.json") or os.path.lexists(app/"Contents/Resources/agent_links.v2.json"):
        from .product_install import build_default_product_installer
        installer = build_default_product_installer()
        return installer

    home = Path.home()
    state_root = home / "Library/Application Support/Lulu/AgentEntry"
    dmg_receipt = state_root / "verified-dmg.json"
    app_bundle = Path("/Applications/Lulu.app")
    if dmg is not None:
        provision_verified_dmg_receipt(
            Path(dmg),
            expected_app=app_bundle,
            receipt_path=dmg_receipt,
        )
    try:
        receipt = _load_dmg_receipt(dmg_receipt)
        identity = receipt["release_dmg_identity"]
        if type(identity) is not dict or type(identity.get("path")) is not str:
            raise RuntimeError("PACKAGE_VERIFICATION_RECEIPT_INVALID")
        selected_dmg = Path(str(identity["path"]))
        _verify_dmg_binding(selected_dmg, receipt)
    except Exception as exc:
        if dmg is not None:
            raise
        raise DmgSelectionRequired() from exc
    # An App update preserves the previous DMG and its valid receipt. Compare
    # against the current App before choosing the existing installation path.
    verified = verify_component_manifest(
        app_bundle, app_bundle / "Contents/Resources/agent_product_manifest.json"
    )
    if any(receipt.get(field) != verified.get(field) for field in _DMG_APP_BINDING_FIELDS):
        raise DmgSelectionRequired()
    return FormalAgentEntryInstaller(
        FormalInstallPaths(
            app_bundle=app_bundle,
            dmg=selected_dmg,
            dmg_receipt=dmg_receipt,
            state_root=state_root,
            launch_agent_target=home
            / "Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
            skill_target=home / ".agents/skills/lulu-agent",
            launcher_target=home / ".local/bin/lulu",
        ),
        key_store=StrictKeyringStore(),
        service=CurrentUserLaunchctlService(),
        installation_id_factory=lambda: f"lulu-agent-{secrets.token_urlsafe(24)}",
    )


def inspect_default_agent_skill_status() -> str:
    """Read the current bundled-Skill status without selecting a DMG or writing state."""

    from ..license.store import StrictKeyringStore
    from .effects import CurrentUserLaunchctlService

    from .standalone_source import DESCRIPTOR
    import sys
    frozen_app = Path(sys.executable).parent.parent.parent
    if getattr(sys, "frozen", False) and (frozen_app/DESCRIPTOR).exists():
        app = frozen_app
    else:
        app = Path("/Applications/Lulu.app")
    if os.path.lexists(app/DESCRIPTOR) or os.path.lexists(app/"Contents/Resources/component_delivery.v2.json") or os.path.lexists(app/"Contents/Resources/agent_links.v2.json"):
        from .product_install import build_default_product_installer
        installer = build_default_product_installer()
        return installer.skill_status()

    home = Path.home()
    state_root = home / "Library/Application Support/Lulu/AgentEntry"
    return FormalAgentEntryInstaller(
        FormalInstallPaths(
            app_bundle=Path("/Applications/Lulu.app"),
            dmg=Path("/private/var/empty/lulu-agent-status.dmg"),
            dmg_receipt=state_root / "verified-dmg.json",
            state_root=state_root,
            launch_agent_target=home
            / "Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
            skill_target=home / ".agents/skills/lulu-agent",
            launcher_target=home / ".local/bin/lulu",
        ),
        key_store=StrictKeyringStore(),
        service=CurrentUserLaunchctlService(),
        installation_id_factory=lambda: "status-is-read-only",
    ).status()
