"""Thin ctypes boundary for the macOS XPC measured-process bridge."""

from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
from typing import Callable

from .handshake import HandshakeError, PeerProcessEvidence, PeerRequirement


_TOKEN_BYTES = 32
_PATH_BYTES = 4096


class NativePeerFactsV1(ctypes.Structure):
    _fields_ = [
        ("schema_version", ctypes.c_uint32),
        ("pid", ctypes.c_int32),
        ("effective_uid", ctypes.c_uint32),
        ("effective_gid", ctypes.c_uint32),
        ("audit_uid", ctypes.c_uint32),
        ("audit_session_id", ctypes.c_uint32),
        ("pidversion", ctypes.c_int32),
        ("device", ctypes.c_uint64),
        ("inode", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("mode", ctypes.c_uint32),
        ("connection_instance", ctypes.c_uint64),
        ("audit_token", ctypes.c_uint8 * _TOKEN_BYTES),
        ("executable_path", ctypes.c_char * _PATH_BYTES),
    ]


class XpcTransportError(RuntimeError):
    def __init__(self, native_code: int, reason_code: str = "COMPONENT_EVIDENCE_REQUIRED") -> None:
        super().__init__(reason_code)
        self.native_code = native_code
        self.reason_code = reason_code
        self.effect_count = 0
        # Closed diagnostic only; preserve the public failure reason and fail-closed path.
        kinds = {
            2: "invalid_argument", 10: "audit_token_unavailable", 11: "peer_mismatch",
            12: "process_unavailable", 20: "connection_failed", 21: "timeout",
            22: "protocol_error", 30: "handler_error",
        }
        if type(native_code) is int:
            self.add_note(f"native_code={native_code}; native_kind={kinds.get(native_code, 'unknown')}")


def evidence_from_native_facts(
    facts: NativePeerFactsV1,
    *,
    requirement: PeerRequirement,
    connection_id: str,
) -> PeerProcessEvidence:
    """Convert only kernel/XPC-derived facts after exact component matching."""

    try:
        executable_path = bytes(facts.executable_path).split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED") from exc
    actual = {
        "schema_version": int(facts.schema_version),
        "executable_path": executable_path,
        "device": int(facts.device),
        "inode": int(facts.inode),
        "size": int(facts.size),
        "mode": int(facts.mode),
        "effective_uid": int(facts.effective_uid),
        "audit_uid": int(facts.audit_uid),
        "audit_session_id": int(facts.audit_session_id),
    }
    expected = {
        "schema_version": 1,
        "executable_path": requirement.executable_path,
        "device": requirement.device,
        "inode": requirement.inode,
        "size": requirement.size,
        "mode": requirement.mode,
        "effective_uid": requirement.user_id,
        "audit_uid": requirement.user_id,
        "audit_session_id": requirement.audit_session_id,
    }
    if actual != expected or facts.pid <= 0 or facts.pidversion <= 0 or facts.connection_instance <= 0:
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
    token = bytes(facts.audit_token)
    return PeerProcessEvidence.from_xpc_system_facts(
        requirement=requirement,
        audit_token_digest=hashlib.sha256(token).hexdigest(),
        pid=int(facts.pid),
        pidversion=int(facts.pidversion),
        connection_id=connection_id,
        transport_connection_instance=int(facts.connection_instance),
    )


class XpcBridge:
    def __init__(self, library_path: Path) -> None:
        path = Path(library_path)
        if not path.is_absolute():
            raise XpcTransportError(2)
        try:
            library = ctypes.CDLL(str(path))
        except OSError as exc:
            raise XpcTransportError(10) from exc
        library.lulu_xpc_runtime_available.argtypes = []
        library.lulu_xpc_runtime_available.restype = ctypes.c_int
        library.lulu_xpc_current_audit_session.argtypes = []
        library.lulu_xpc_current_audit_session.restype = ctypes.c_uint32
        library.lulu_xpc_client_open.argtypes = [ctypes.c_char_p]
        library.lulu_xpc_client_open.restype = ctypes.c_void_p
        library.lulu_xpc_client_exchange.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(NativePeerFactsV1),
            ctypes.c_uint32,
        ]
        library.lulu_xpc_client_exchange.restype = ctypes.c_int
        library.lulu_xpc_client_close.argtypes = [ctypes.c_void_p]
        library.lulu_xpc_client_close.restype = None
        self._callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.POINTER(NativePeerFactsV1),
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
            ctypes.c_void_p,
        )
        library.lulu_xpc_server_run.argtypes = [
            ctypes.c_char_p,
            self._callback_type,
            ctypes.c_void_p,
        ]
        library.lulu_xpc_server_run.restype = ctypes.c_int
        self._library = library
        if library.lulu_xpc_runtime_available() != 1:
            raise XpcTransportError(10)

    @property
    def audit_session_id(self) -> int:
        value = int(self._library.lulu_xpc_current_audit_session())
        if value <= 0:
            raise XpcTransportError(10)
        return value

    def connect(self, service_name: str) -> "XpcClient":
        if not service_name or "\0" in service_name:
            raise XpcTransportError(2)
        handle = self._library.lulu_xpc_client_open(service_name.encode("utf-8"))
        if not handle:
            raise XpcTransportError(20)
        return XpcClient(self._library, handle)

    def run_server(
        self,
        service_name: str,
        handler: Callable[[dict[str, object], NativePeerFactsV1], dict[str, object]],
    ) -> None:
        if not service_name or "\0" in service_name:
            raise XpcTransportError(2)

        @self._callback_type
        def callback(request, peer, response, capacity, _context):
            try:
                payload = json.loads(request.decode("utf-8"))
                if type(payload) is not dict:
                    return 22
                result = handler(payload, peer.contents)
                if type(result) is not dict:
                    return 30
                encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(encoded) + 1 > capacity:
                    return 22
                ctypes.memmove(response, encoded + b"\0", len(encoded) + 1)
                return 0
            except Exception as exc:
                from ..diagnostics import record_failure
                record_failure("core_request", exc)
                return 30

        result = self._library.lulu_xpc_server_run(
            service_name.encode("utf-8"),
            callback,
            None,
        )
        raise XpcTransportError(result)


class XpcClient:
    def __init__(self, library: ctypes.CDLL, handle: int) -> None:
        self._library = library
        self._handle = handle

    def exchange(self, payload: dict[str, object], *, timeout_ms: int = 5000) -> tuple[dict[str, object], NativePeerFactsV1]:
        if self._handle is None or type(payload) is not dict:
            raise XpcTransportError(2)
        request = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(request) > 4 * 1024 * 1024 - 1:
            raise XpcTransportError(22)
        response = ctypes.create_string_buffer(4 * 1024 * 1024)
        peer = NativePeerFactsV1()
        result = self._library.lulu_xpc_client_exchange(
            self._handle,
            request,
            response,
            len(response),
            ctypes.byref(peer),
            timeout_ms,
        )
        if result != 0:
            raise XpcTransportError(result)
        try:
            decoded = json.loads(response.value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise XpcTransportError(22) from exc
        if type(decoded) is not dict:
            raise XpcTransportError(22)
        return decoded, peer

    def close(self) -> None:
        if self._handle is not None:
            self._library.lulu_xpc_client_close(self._handle)
            self._handle = None

    def __enter__(self) -> "XpcClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
