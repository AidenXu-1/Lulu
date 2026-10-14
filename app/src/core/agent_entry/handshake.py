"""H0/H1/H2 handshake bound to XPC-provided live process evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[0-9a-f]{40}$")
_ROLE = frozenset({"gui", "agent_cli", "agent_core"})


class HandshakeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.effect_count = 0


def _digest_file(path: Path) -> tuple[str, os.stat_result]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                current.st_dev,
                current.st_ino,
                current.st_mode,
            ):
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        finally:
            os.close(descriptor)
        return digest.hexdigest(), current
    except HandshakeError:
        raise
    except OSError as exc:
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED") from exc


@dataclass(frozen=True, slots=True)
class PeerRequirement:
    role: str
    executable_path: str
    device: int
    inode: int
    owner: int
    mode: int
    size: int
    sha256: str
    user_id: int
    audit_session_id: int
    product_version: str
    product_build: str
    source_revision: str
    generation: int
    manifest_sha256: str
    catalog_sha256: str
    protocol_version: str

    @classmethod
    def from_file(
        cls,
        *,
        role: str,
        path: Path,
        user_id: int,
        audit_session_id: int,
        product_version: str,
        product_build: str,
        source_revision: str,
        generation: int,
        manifest_sha256: str,
        catalog_sha256: str,
        protocol_version: str,
    ) -> "PeerRequirement":
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("peer executable path must be absolute")
        digest, metadata = _digest_file(target)
        requirement = cls(
            role=role,
            executable_path=str(target),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            owner=metadata.st_uid,
            mode=stat.S_IMODE(metadata.st_mode),
            size=metadata.st_size,
            sha256=digest,
            user_id=user_id,
            audit_session_id=audit_session_id,
            product_version=product_version,
            product_build=product_build,
            source_revision=source_revision,
            generation=generation,
            manifest_sha256=manifest_sha256,
            catalog_sha256=catalog_sha256,
            protocol_version=protocol_version,
        )
        requirement._validate()
        return requirement

    def _validate(self) -> None:
        if (
            self.role not in _ROLE
            or not Path(self.executable_path).is_absolute()
            or self.device < 0
            or self.inode <= 0
            or self.owner < 0
            or self.mode & 0o022
            or self.size <= 0
            or not _SHA256.fullmatch(self.sha256)
            or self.user_id < 0
            or self.audit_session_id < 0
            or not self.product_version
            or not self.product_build
            or not _SOURCE.fullmatch(self.source_revision)
            or self.generation != 9
            or not _SHA256.fullmatch(self.manifest_sha256)
            or not _SHA256.fullmatch(self.catalog_sha256)
            or self.protocol_version != "1.0.0"
        ):
            raise ValueError("invalid peer requirement")


@dataclass(frozen=True, slots=True)
class PeerProcessEvidence:
    provenance: str
    role: str
    executable_path: str
    device: int
    inode: int
    owner: int
    mode: int
    size: int
    sha256: str
    effective_user_id: int
    audit_user_id: int
    audit_session_id: int
    pid: int
    pidversion: int
    audit_token_digest: str
    connection_id: str
    transport_connection_instance: int
    product_version: str
    product_build: str
    source_revision: str
    generation: int
    manifest_sha256: str
    catalog_sha256: str
    protocol_version: str
    measurement_digest: str

    @classmethod
    def from_xpc_system_facts(
        cls,
        *,
        requirement: PeerRequirement,
        audit_token_digest: str,
        pid: int,
        pidversion: int,
        connection_id: str,
        transport_connection_instance: int = 1,
    ) -> "PeerProcessEvidence":
        requirement._validate()
        values: dict[str, Any] = {
            "provenance": "xpc_connection_audit_token",
            "role": requirement.role,
            "executable_path": requirement.executable_path,
            "device": requirement.device,
            "inode": requirement.inode,
            "owner": requirement.owner,
            "mode": requirement.mode,
            "size": requirement.size,
            "sha256": requirement.sha256,
            "effective_user_id": requirement.user_id,
            "audit_user_id": requirement.user_id,
            "audit_session_id": requirement.audit_session_id,
            "pid": pid,
            "pidversion": pidversion,
            "audit_token_digest": audit_token_digest,
            "connection_id": connection_id,
            "transport_connection_instance": transport_connection_instance,
            "product_version": requirement.product_version,
            "product_build": requirement.product_build,
            "source_revision": requirement.source_revision,
            "generation": requirement.generation,
            "manifest_sha256": requirement.manifest_sha256,
            "catalog_sha256": requirement.catalog_sha256,
            "protocol_version": requirement.protocol_version,
        }
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        values["measurement_digest"] = hashlib.sha256(canonical).hexdigest()
        return cls(**values)


def _verify_peer(
    evidence: object,
    requirement: PeerRequirement,
    *,
    connection_id: str,
    reason_code: str,
) -> PeerProcessEvidence:
    if type(evidence) is not PeerProcessEvidence:
        raise HandshakeError(reason_code)
    peer = evidence
    if (
        peer.provenance != "xpc_connection_audit_token"
        or peer.connection_id != connection_id
        or peer.pid <= 0
        or peer.pidversion <= 0
        or not _SHA256.fullmatch(peer.audit_token_digest)
        or peer.transport_connection_instance <= 0
    ):
        raise HandshakeError(reason_code)
    expected_values = {
        "role": requirement.role,
        "executable_path": requirement.executable_path,
        "device": requirement.device,
        "inode": requirement.inode,
        "owner": requirement.owner,
        "mode": requirement.mode,
        "size": requirement.size,
        "sha256": requirement.sha256,
        "effective_user_id": requirement.user_id,
        "audit_user_id": requirement.user_id,
        "audit_session_id": requirement.audit_session_id,
        "product_version": requirement.product_version,
        "product_build": requirement.product_build,
        "source_revision": requirement.source_revision,
        "generation": requirement.generation,
        "manifest_sha256": requirement.manifest_sha256,
        "catalog_sha256": requirement.catalog_sha256,
        "protocol_version": requirement.protocol_version,
    }
    if any(getattr(peer, key) != value for key, value in expected_values.items()):
        raise HandshakeError(reason_code)
    digest, metadata = _digest_file(Path(requirement.executable_path))
    if (
        digest != requirement.sha256
        or metadata.st_dev != requirement.device
        or metadata.st_ino != requirement.inode
        or metadata.st_uid != requirement.owner
        or stat.S_IMODE(metadata.st_mode) != requirement.mode
        or metadata.st_size != requirement.size
    ):
        raise HandshakeError(reason_code)
    values = {
        key: getattr(peer, key)
        for key in PeerProcessEvidence.__dataclass_fields__
        if key != "measurement_digest"
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != peer.measurement_digest:
        raise HandshakeError(reason_code)
    return peer


class MeasuredPeerReceipt:
    __slots__ = (
        "_connection_id",
        "_challenge",
        "_client_requirement",
        "_core_requirement",
        "_client_digest",
        "_core_digest",
        "_closed",
    )

    def __init__(
        self,
        *,
        connection_id: str,
        challenge: str,
        client_requirement: PeerRequirement,
        core_requirement: PeerRequirement,
        client_digest: str,
        core_digest: str,
    ) -> None:
        self._connection_id = connection_id
        self._challenge = challenge
        self._client_requirement = client_requirement
        self._core_requirement = core_requirement
        self._client_digest = client_digest
        self._core_digest = core_digest
        self._closed = False

    def __reduce__(self) -> object:
        raise TypeError("MeasuredPeerReceipt is connection-local and non-serializable")

    def close(self) -> None:
        self._closed = True

    def dispatch(
        self,
        request: Mapping[str, object],
        *,
        current_client: object,
        current_core: object,
        callback: Callable[[dict[str, object]], object],
    ) -> object:
        if self._closed:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        client = _verify_peer(
            current_client,
            self._client_requirement,
            connection_id=self._connection_id,
            reason_code="COMPONENT_EVIDENCE_REQUIRED",
        )
        core = _verify_peer(
            current_core,
            self._core_requirement,
            connection_id=self._connection_id,
            reason_code="CORE_COMPONENT_MISMATCH",
        )
        if client.measurement_digest != self._client_digest or core.measurement_digest != self._core_digest:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        if type(request) is not dict or frozenset(request) != {"command", "arguments"}:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        if type(request.get("command")) is not str or type(request.get("arguments")) is not dict:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        return callback(dict(request))


class MutualMeasuredHandshake:
    def __init__(
        self,
        *,
        connection_id: str,
        challenge: str,
        client_requirement: PeerRequirement,
        core_requirement: PeerRequirement,
    ) -> None:
        if not connection_id or len(challenge) < 16:
            raise ValueError("connection binding is invalid")
        self._connection_id = connection_id
        self._challenge = challenge
        self._client_requirement = client_requirement
        self._core_requirement = core_requirement
        self._phase = "created"
        self._client: PeerProcessEvidence | None = None
        self._core: PeerProcessEvidence | None = None

    def _exact(self, message: Mapping[str, object], expected: dict[str, object]) -> None:
        if type(message) is not dict or dict(message) != expected:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")

    def accept_h0(
        self,
        message: Mapping[str, object],
        *,
        client_evidence: object,
        core_evidence: object,
    ) -> None:
        if self._phase != "created":
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        self._exact(
            message,
            {"phase": "H0", "connection_id": self._connection_id, "challenge": self._challenge},
        )
        self._client = _verify_peer(
            client_evidence,
            self._client_requirement,
            connection_id=self._connection_id,
            reason_code="COMPONENT_EVIDENCE_REQUIRED",
        )
        self._core = _verify_peer(
            core_evidence,
            self._core_requirement,
            connection_id=self._connection_id,
            reason_code="CORE_COMPONENT_MISMATCH",
        )
        self._phase = "h0"

    def accept_h1(self, message: Mapping[str, object]) -> None:
        if self._phase != "h0" or self._client is None or self._core is None:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        self._exact(
            message,
            {
                "phase": "H1",
                "connection_id": self._connection_id,
                "challenge": self._challenge,
                "client_measurement": self._client.measurement_digest,
                "core_measurement": self._core.measurement_digest,
            },
        )
        self._phase = "h1"

    def accept_h2(self, message: Mapping[str, object]) -> MeasuredPeerReceipt:
        if self._phase != "h1" or self._client is None or self._core is None:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        self._exact(
            message,
            {
                "phase": "H2",
                "connection_id": self._connection_id,
                "challenge": self._challenge,
                "manifest_sha256": self._client_requirement.manifest_sha256,
                "catalog_sha256": self._client_requirement.catalog_sha256,
                "generation": self._client_requirement.generation,
            },
        )
        self._phase = "complete"
        return MeasuredPeerReceipt(
            connection_id=self._connection_id,
            challenge=self._challenge,
            client_requirement=self._client_requirement,
            core_requirement=self._core_requirement,
            client_digest=self._client.measurement_digest,
            core_digest=self._core.measurement_digest,
        )
