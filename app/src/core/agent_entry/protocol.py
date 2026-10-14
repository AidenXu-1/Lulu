"""Generation-9 H0/H1/H2 protocol over one measured XPC connection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import secrets

from .handshake import HandshakeError, PeerProcessEvidence, PeerRequirement
from .native_transport import NativePeerFactsV1, evidence_from_native_facts

_HANDSHAKE_TIMEOUT_MS = 5000
_CORE_START_TIMEOUT_MS = 60000
_BUSINESS_TIMEOUT_MS = 60000
_LOCAL_TRANSCRIPTION_TIMEOUT_MS = 30 * 60 * 1000


def _exact(payload: object, fields: frozenset[str]) -> dict[str, object]:
    if type(payload) is not dict or frozenset(payload) != fields:
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
    return payload


def _same_process(left: PeerProcessEvidence, right: PeerProcessEvidence) -> bool:
    return (
        left.measurement_digest == right.measurement_digest
        and left.pid == right.pid
        and left.pidversion == right.pidversion
        and left.audit_token_digest == right.audit_token_digest
        and left.transport_connection_instance == right.transport_connection_instance
    )


@dataclass(slots=True)
class _ServerConnection:
    phase: str
    client_challenge: str
    core_challenge: str
    client_evidence: PeerProcessEvidence
    client_requirement: PeerRequirement


class AgentCoreProtocol:
    """Core-side state; dispatcher is unreachable until H2 succeeds."""

    def __init__(
        self,
        *,
        client_requirement: PeerRequirement | tuple[PeerRequirement, ...],
        dispatcher: Callable[[dict[str, object]], object],
        challenge_factory: Callable[[], str] | None = None,
    ) -> None:
        requirements = (
            client_requirement
            if isinstance(client_requirement, tuple)
            else (client_requirement,)
        )
        if not requirements or any(
            not isinstance(requirement, PeerRequirement)
            for requirement in requirements
        ):
            raise TypeError("client requirement is invalid")
        self._requirements = requirements
        self._dispatcher = dispatcher
        self._challenge_factory = challenge_factory or (lambda: secrets.token_hex(32))
        self._connections: dict[str, _ServerConnection] = {}

    @staticmethod
    def _evidence(
        facts: NativePeerFactsV1,
        connection_id: str,
        requirement: PeerRequirement,
    ) -> PeerProcessEvidence:
        return evidence_from_native_facts(
            facts,
            requirement=requirement,
            connection_id=connection_id,
        )

    def _select_evidence(
        self,
        facts: NativePeerFactsV1,
        connection_id: str,
    ) -> tuple[PeerProcessEvidence, PeerRequirement]:
        for requirement in self._requirements:
            try:
                return self._evidence(facts, connection_id, requirement), requirement
            except HandshakeError:
                continue
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")

    def handle(self, payload: dict[str, object], facts: NativePeerFactsV1) -> dict[str, object]:
        if type(payload) is not dict or type(payload.get("phase")) is not str:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        phase = str(payload["phase"])
        if phase == "H0":
            message = _exact(payload, frozenset({"phase", "connection_id", "challenge"}))
            connection_id = message.get("connection_id")
            challenge = message.get("challenge")
            if (
                type(connection_id) is not str
                or len(connection_id) < 24
                or type(challenge) is not str
                or len(challenge) < 32
                or connection_id in self._connections
            ):
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            evidence, requirement = self._select_evidence(facts, connection_id)
            core_challenge = self._challenge_factory()
            if type(core_challenge) is not str or len(core_challenge) < 32:
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            self._connections[connection_id] = _ServerConnection(
                phase="h0",
                client_challenge=challenge,
                core_challenge=core_challenge,
                client_evidence=evidence,
                client_requirement=requirement,
            )
            return {
                "phase": "H0_ACK",
                "connection_id": connection_id,
                "challenge": challenge,
                "core_challenge": core_challenge,
                "client_measurement": evidence.measurement_digest,
            }

        connection_id = payload.get("connection_id")
        if type(connection_id) is not str or connection_id not in self._connections:
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
        state = self._connections[connection_id]
        current = self._evidence(
            facts,
            connection_id,
            state.client_requirement,
        )
        if not _same_process(current, state.client_evidence):
            raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")

        if phase == "H1":
            message = _exact(
                payload,
                frozenset({"phase", "connection_id", "challenge", "core_challenge", "client_measurement", "core_measurement"}),
            )
            if (
                state.phase != "h0"
                or message.get("challenge") != state.client_challenge
                or message.get("core_challenge") != state.core_challenge
                or message.get("client_measurement") != current.measurement_digest
                or type(message.get("core_measurement")) is not str
            ):
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            state.phase = "h1"
            return {"phase": "H1_ACK", "connection_id": connection_id}

        if phase == "H2":
            message = _exact(
                payload,
                frozenset({"phase", "connection_id", "manifest_sha256", "catalog_sha256", "generation", "protocol_version"}),
            )
            if (
                state.phase != "h1"
                or message.get("manifest_sha256") != state.client_requirement.manifest_sha256
                or message.get("catalog_sha256") != state.client_requirement.catalog_sha256
                or message.get("generation") != state.client_requirement.generation
                or message.get("protocol_version") != state.client_requirement.protocol_version
            ):
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            state.phase = "ready"
            return {"phase": "H2_ACK", "connection_id": connection_id}

        if phase == "BUSINESS":
            message = _exact(
                payload,
                frozenset({"phase", "connection_id", "command", "arguments"}),
            )
            if (
                state.phase != "ready"
                or type(message.get("command")) is not str
                or type(message.get("arguments")) is not dict
            ):
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            if message['command'].startswith('gui.') and state.client_requirement.role != 'gui':
                raise HandshakeError('COMPONENT_EVIDENCE_REQUIRED')
            result = self._dispatcher(
                {"command": message["command"], "arguments": dict(message["arguments"])}
            )
            if type(result) is not dict:
                raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")
            return result
        raise HandshakeError("COMPONENT_EVIDENCE_REQUIRED")


class MeasuredCoreClient:
    """CLI-side handshake and reverse Core measurement on every reply."""

    def __init__(
        self,
        *,
        exchange: object,
        client_requirement: PeerRequirement,
        core_requirement: PeerRequirement,
        connection_id: str | None = None,
        challenge: str | None = None,
    ) -> None:
        self._exchange = exchange
        self._client_requirement = client_requirement
        self._core_requirement = core_requirement
        self._connection_id = connection_id or f"connection-{secrets.token_hex(24)}"
        self._challenge = challenge or secrets.token_hex(32)
        self._core_evidence: PeerProcessEvidence | None = None
        self._ready = False
        self.business_dispatched = False

    def _send(
        self, payload: dict[str, object], *, timeout_ms: int = _HANDSHAKE_TIMEOUT_MS
    ) -> dict[str, object]:
        response, facts = self._exchange.exchange(payload, timeout_ms=timeout_ms)
        current = evidence_from_native_facts(
            facts,
            requirement=self._core_requirement,
            connection_id=self._connection_id,
        )
        if self._core_evidence is None:
            self._core_evidence = current
        elif not _same_process(current, self._core_evidence):
            raise HandshakeError("CORE_COMPONENT_MISMATCH")
        return response

    def _handshake(self) -> None:
        h0 = self._send(
            {"phase": "H0", "connection_id": self._connection_id, "challenge": self._challenge},
            # launchd may be starting Core and verifying the complete bundle.
            # Wait for this single handshake; never replay a business request.
            timeout_ms=_CORE_START_TIMEOUT_MS,
        )
        if (
            frozenset(h0) != {"phase", "connection_id", "challenge", "core_challenge", "client_measurement"}
            or h0.get("phase") != "H0_ACK"
            or h0.get("connection_id") != self._connection_id
            or h0.get("challenge") != self._challenge
            or type(h0.get("core_challenge")) is not str
            or self._core_evidence is None
        ):
            raise HandshakeError("CORE_COMPONENT_MISMATCH")
        h1 = self._send(
            {
                "phase": "H1",
                "connection_id": self._connection_id,
                "challenge": self._challenge,
                "core_challenge": h0["core_challenge"],
                "client_measurement": h0["client_measurement"],
                "core_measurement": self._core_evidence.measurement_digest,
            }
        )
        if h1 != {"phase": "H1_ACK", "connection_id": self._connection_id}:
            raise HandshakeError("CORE_COMPONENT_MISMATCH")
        h2 = self._send(
            {
                "phase": "H2",
                "connection_id": self._connection_id,
                "manifest_sha256": self._client_requirement.manifest_sha256,
                "catalog_sha256": self._client_requirement.catalog_sha256,
                "generation": self._client_requirement.generation,
                "protocol_version": self._client_requirement.protocol_version,
            }
        )
        if h2 != {"phase": "H2_ACK", "connection_id": self._connection_id}:
            raise HandshakeError("CORE_COMPONENT_MISMATCH")
        self._ready = True

    def dispatch(self, command: str, arguments: dict[str, object]) -> dict[str, object]:
        if not self._ready:
            self._handshake()
        self.business_dispatched = True
        return self._send(
            {
                "phase": "BUSINESS",
                "connection_id": self._connection_id,
                "command": command,
                "arguments": dict(arguments),
            },
            # Local ASR returns the complete result synchronously. Long audio
            # can exceed one minute while its durable job keeps running.
            # Only this exact CLI operation receives a longer bounded wait;
            # handshake, polling and unknown-outcome/no-replay rules stay intact.
            timeout_ms=(_LOCAL_TRANSCRIPTION_TIMEOUT_MS if command == 'argv'
                and type(arguments.get('argv')) is list
                and arguments['argv'][:2] == ['transcribe', 'run']
                else _BUSINESS_TIMEOUT_MS),
        )
