from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class Generation9XpcTransportTests(unittest.TestCase):
    def test_large_batch_response_is_not_truncated_at_64_kib(self):
        import json
        from src.core.agent_entry.native_transport import XpcClient, XpcTransportError
        payload = {"jobs": [{"text": "x" * 512} for _ in range(289)]}
        raw = json.dumps(payload).encode() + b"\0"
        capacities = []
        class Library:
            def lulu_xpc_client_exchange(self, handle, request, response, capacity, peer, timeout):
                capacities.append(capacity)
                if len(raw) > capacity:
                    return 22
                ctypes.memmove(response, raw, len(raw))
                return 0
        client = XpcClient(Library(), 1)
        result, _peer = client.exchange({"command": "gui.jobs.list"})
        self.assertEqual(result, payload)
        self.assertEqual(capacities, [4 * 1024 * 1024])
        with self.assertRaises(XpcTransportError):
            client.exchange({"oversized": "x" * (4 * 1024 * 1024)})

    def test_native_facts_are_required_to_match_the_component_requirement(self) -> None:
        from src.core.agent_entry.handshake import PeerRequirement
        from src.core.agent_entry.native_transport import (
            NativePeerFactsV1,
            evidence_from_native_facts,
        )

        with TemporaryDirectory() as temporary:
            executable = Path(temporary) / "lulu-agent-cli"
            executable.write_bytes(b"cli\n")
            executable.chmod(0o700)
            metadata = executable.stat()
            requirement = PeerRequirement.from_file(
                role="agent_cli",
                path=executable,
                user_id=os.getuid(),
                audit_session_id=101,
                product_version="0.3.1",
                product_build="20260815.3",
                source_revision="a" * 40,
                generation=9,
                manifest_sha256="b" * 64,
                catalog_sha256="c" * 64,
                protocol_version="1.0.0",
            )
            facts = NativePeerFactsV1()
            facts.schema_version = 1
            facts.pid = 123
            facts.effective_uid = os.getuid()
            facts.audit_uid = os.getuid()
            facts.audit_session_id = 101
            facts.pidversion = 77
            facts.device = metadata.st_dev
            facts.inode = metadata.st_ino
            facts.size = metadata.st_size
            facts.mode = 0o700
            facts.connection_instance = 999
            token = bytes(range(32))
            ctypes.memmove(facts.audit_token, token, len(token))
            facts.executable_path = os.fsencode(executable)

            evidence = evidence_from_native_facts(
                facts,
                requirement=requirement,
                connection_id="connection-1",
            )
            self.assertEqual(evidence.audit_token_digest, hashlib.sha256(token).hexdigest())
            self.assertEqual(evidence.pidversion, 77)
            self.assertEqual(evidence.transport_connection_instance, 999)

            facts.inode += 1
            with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED"):
                evidence_from_native_facts(
                    facts,
                    requirement=requirement,
                    connection_id="connection-1",
                )

    def test_bridge_error_has_zero_effect_and_no_fallback(self) -> None:
        from src.core.agent_entry.native_transport import XpcTransportError

        error = XpcTransportError(20)
        self.assertEqual(error.reason_code, "COMPONENT_EVIDENCE_REQUIRED")
        self.assertEqual(error.effect_count, 0)
        self.assertFalse(hasattr(error, "fallback"))

    def test_traceback_distinguishes_native_failure_without_changing_reason(self) -> None:
        import traceback
        from src.core.agent_entry.native_transport import XpcTransportError

        for code, kind in ((21, "timeout"), (20, "connection_failed"), (30, "handler_error"), (99, "unknown")):
            with self.subTest(code=code):
                error = XpcTransportError(code)
                self.assertEqual(str(error), "COMPONENT_EVIDENCE_REQUIRED")
                self.assertIn(f"native_code={code}; native_kind={kind}", "".join(traceback.format_exception_only(error)))


if __name__ == "__main__":
    unittest.main()
