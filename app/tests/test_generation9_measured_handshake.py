from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.core.agent_entry.handshake import (
    HandshakeError,
    MutualMeasuredHandshake,
    PeerProcessEvidence,
    PeerRequirement,
)


MANIFEST_SHA = "1" * 64
CATALOG_SHA = "2" * 64


class Generation9MeasuredHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli = root / "lulu-agent-cli"
        self.core = root / "lulu-agent-core"
        self.cli.write_bytes(b"cli-generation-9")
        self.core.write_bytes(b"core-generation-9")
        self.cli.chmod(0o755)
        self.core.chmod(0o755)
        self.cli_requirement = PeerRequirement.from_file(
            role="agent_cli",
            path=self.cli,
            user_id=501,
            audit_session_id=44,
            product_version="0.3.2",
            product_build="20260823.4",
            source_revision="7b15ff66cd214786293d62ffde6ba80276a5d891",
            generation=9,
            manifest_sha256=MANIFEST_SHA,
            catalog_sha256=CATALOG_SHA,
            protocol_version="1.0.0",
        )
        self.core_requirement = PeerRequirement.from_file(
            role="agent_core",
            path=self.core,
            user_id=501,
            audit_session_id=44,
            product_version="0.3.2",
            product_build="20260823.4",
            source_revision="7b15ff66cd214786293d62ffde6ba80276a5d891",
            generation=9,
            manifest_sha256=MANIFEST_SHA,
            catalog_sha256=CATALOG_SHA,
            protocol_version="1.0.0",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evidence(self, requirement: PeerRequirement, *, pid: int, pidversion: int) -> PeerProcessEvidence:
        return PeerProcessEvidence.from_xpc_system_facts(
            requirement=requirement,
            audit_token_digest=("a" if requirement.role == "agent_cli" else "b") * 64,
            pid=pid,
            pidversion=pidversion,
            connection_id="connection-001",
        )

    def handshake(self) -> MutualMeasuredHandshake:
        return MutualMeasuredHandshake(
            connection_id="connection-001",
            challenge="challenge-0123456789abcdef",
            client_requirement=self.cli_requirement,
            core_requirement=self.core_requirement,
        )

    def complete(self, handshake: MutualMeasuredHandshake):
        client = self.evidence(self.cli_requirement, pid=101, pidversion=7)
        core = self.evidence(self.core_requirement, pid=202, pidversion=9)
        handshake.accept_h0(
            {"phase": "H0", "connection_id": "connection-001", "challenge": "challenge-0123456789abcdef"},
            client_evidence=client,
            core_evidence=core,
        )
        handshake.accept_h1(
            {
                "phase": "H1",
                "connection_id": "connection-001",
                "challenge": "challenge-0123456789abcdef",
                "client_measurement": client.measurement_digest,
                "core_measurement": core.measurement_digest,
            }
        )
        return handshake.accept_h2(
            {
                "phase": "H2",
                "connection_id": "connection-001",
                "challenge": "challenge-0123456789abcdef",
                "manifest_sha256": MANIFEST_SHA,
                "catalog_sha256": CATALOG_SHA,
                "generation": 9,
            }
        )

    def test_two_way_message_bound_measurement_allows_dispatch_only_after_h2(self) -> None:
        handshake = self.handshake()
        receipt = self.complete(handshake)
        calls: list[dict[str, object]] = []
        result = receipt.dispatch(
            {"command": "version", "arguments": {}},
            current_client=self.evidence(self.cli_requirement, pid=101, pidversion=7),
            current_core=self.evidence(self.core_requirement, pid=202, pidversion=9),
            callback=lambda request: calls.append(request) or {"ok": True},
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [{"command": "version", "arguments": {}}])
        with self.assertRaises(TypeError):
            json.dumps(receipt)

    def test_business_fields_before_h2_are_rejected_with_zero_effects(self) -> None:
        handshake = self.handshake()
        with self.assertRaises(HandshakeError) as caught:
            handshake.accept_h0(
                {
                    "phase": "H0",
                    "connection_id": "connection-001",
                    "challenge": "challenge-0123456789abcdef",
                    "input_path": "/tmp/secret.wav",
                },
                client_evidence=self.evidence(self.cli_requirement, pid=101, pidversion=7),
                core_evidence=self.evidence(self.core_requirement, pid=202, pidversion=9),
            )
        self.assertEqual(caught.exception.reason_code, "COMPONENT_EVIDENCE_REQUIRED")
        self.assertEqual(caught.exception.effect_count, 0)

    def test_single_claims_and_peer_drift_cannot_authorize(self) -> None:
        handshake = self.handshake()
        for single_claim in (
            {"path": str(self.cli)},
            {"uid": 501},
            {"pid": 101},
            {"owner": 501},
            {"bundle_id": "com.lulu.transcribe.agent-cli"},
            {"version": "0.3.2"},
            {"cdhash": "f" * 40},
        ):
            with self.subTest(single_claim=single_claim):
                with self.assertRaises(HandshakeError):
                    handshake.accept_h0(
                        {"phase": "H0", "connection_id": "connection-001", "challenge": "challenge-0123456789abcdef"},
                        client_evidence=single_claim,
                        core_evidence=self.evidence(self.core_requirement, pid=202, pidversion=9),
                    )

        receipt = self.complete(self.handshake())
        with self.assertRaises(HandshakeError) as caught:
            receipt.dispatch(
                {"command": "version", "arguments": {}},
                current_client=self.evidence(self.cli_requirement, pid=101, pidversion=8),
                current_core=self.evidence(self.core_requirement, pid=202, pidversion=9),
                callback=lambda _request: self.fail("effect ran after process drift"),
            )
        self.assertEqual(caught.exception.reason_code, "COMPONENT_EVIDENCE_REQUIRED")
        self.assertEqual(caught.exception.effect_count, 0)

    def test_role_connection_generation_and_replay_mismatches_fail_closed(self) -> None:
        wrong_role = PeerProcessEvidence.from_xpc_system_facts(
            requirement=self.core_requirement,
            audit_token_digest="c" * 64,
            pid=101,
            pidversion=7,
            connection_id="connection-001",
        )
        with self.assertRaises(HandshakeError):
            self.handshake().accept_h0(
                {"phase": "H0", "connection_id": "connection-001", "challenge": "challenge-0123456789abcdef"},
                client_evidence=wrong_role,
                core_evidence=self.evidence(self.core_requirement, pid=202, pidversion=9),
            )

        receipt = self.complete(self.handshake())
        receipt.close()
        with self.assertRaises(HandshakeError):
            receipt.dispatch(
                {"command": "version", "arguments": {}},
                current_client=self.evidence(self.cli_requirement, pid=101, pidversion=7),
                current_core=self.evidence(self.core_requirement, pid=202, pidversion=9),
                callback=lambda _request: self.fail("closed connection replayed"),
            )


if __name__ == "__main__":
    unittest.main()
