"""Phase 4 CLI structured output contract tests."""

from __future__ import annotations

from io import StringIO
import json
import sys
import unittest

from src.agent_cli.contracts import (
    capture_third_party_output,
    write_cli_error,
    write_jsonl_event,
)


class CliStructuredOutputTests(unittest.TestCase):
    def test_error_exit_redaction_and_third_party_capture_share_one_closed_boundary(self) -> None:
        secret = "PHASE4_PRIVATE_TOKEN_VALUE"
        stdout = StringIO()
        stderr = StringIO()

        def noisy_dependency() -> str:
            print(f"dependency stdout {secret}")
            print("Traceback: dependency stderr", file=sys.stderr)
            return "captured-result"

        result, captured = capture_third_party_output(noisy_dependency)
        exit_code = write_cli_error(
            code="INTEGRITY_ERROR",
            command="invalid",
            details={
                "reason_code": "TRUSTED_ENVELOPE_INVALID",
                "token": secret,
                "traceback": "Traceback: private stack",
            },
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(result, "captured-result")
        self.assertIn(secret, captured["stdout"])
        self.assertIn("Traceback", captured["stderr"])
        self.assertEqual(exit_code, 60)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "INTEGRITY_ERROR")
        self.assertEqual(
            payload["error"]["details"],
            {"reason_code": "TRUSTED_ENVELOPE_INVALID"},
        )
        events = StringIO()
        write_jsonl_event(
            {
                "event_schema_id": "lulu.agent-event/v1",
                "sequence": 1,
                "event": "state",
                "payload": {"status": "unavailable"},
            },
            events,
        )
        write_jsonl_event(
            {
                "event_schema_id": "lulu.agent-event/v1",
                "sequence": 2,
                "event": "terminal",
                "payload": {"status": "complete"},
            },
            events,
        )
        self.assertEqual(
            [json.loads(line)["sequence"] for line in events.getvalue().splitlines()],
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
