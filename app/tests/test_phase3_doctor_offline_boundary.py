"""Phase 3 offline doctor and no-secret/no-Qt boundary tests."""

from __future__ import annotations

from io import StringIO
import json
import os
import socket
import sys
import unittest
from unittest.mock import patch
import urllib.request

from src.agent_cli.main import main


class Phase3DoctorOfflineBoundaryTests(unittest.TestCase):
    def test_default_doctor_is_strictly_offline_and_imports_no_business_runtime(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
            patch.object(urllib.request, "urlopen") as urlopen,
        ):
            exit_code = main(
                ["doctor", "--output", "json"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertEqual(create_connection.call_count, 0)
        self.assertEqual(socket_connect.call_count, 0)
        self.assertEqual(urlopen.call_count, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": True,
                "command": "doctor",
                "result": {
                    "formal_cli_state": "unavailable",
                    "overall_status": "unavailable",
                    "local_checks": {
                        "status": "unavailable",
                        "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
                    },
                    "authorization_checks": {
                        "status": "not_requested",
                        "reason_code": "PHASE3_DOCTOR_DOES_NOT_READ_AUTH",
                    },
                    "network_checks": {
                        "status": "not_requested",
                        "reason_code": "NETWORK_PROBE_REQUIRES_EXPLICIT_APPROVAL",
                        "attempt_count": 0,
                    },
                },
            },
        )
        forbidden_prefixes = (
            "PySide6",
            "src.ui",
            "src.core.auth_guard",
            "src.core.license",
            "src.core.pipeline",
            "src.core.transcription_application",
            "src.core.worker",
            "src.core.models",
        )
        self.assertFalse(
            sorted(
                module_name
                for module_name in sys.modules
                if module_name.startswith(forbidden_prefixes)
            )
        )

    def test_network_prepare_is_non_approvable_non_consumable_and_offline(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.object(socket, "create_connection") as create_connection,
            patch.object(socket.socket, "connect") as socket_connect,
            patch.object(urllib.request, "urlopen") as urlopen,
        ):
            exit_code = main(
                ["doctor", "network", "prepare", "--output", "json"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        self.assertEqual(create_connection.call_count, 0)
        self.assertEqual(socket_connect.call_count, 0)
        self.assertEqual(urlopen.call_count, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": True,
                "command": "doctor network prepare",
                "result": {
                    "formal_cli_state": "unavailable",
                    "proposal_kind": "network_diagnostic_proposal",
                    "proposal_version": "1.0.0",
                    "status": "proposal_only",
                    "approvable": False,
                    "consumable": False,
                    "executable": False,
                    "network_attempt_count": 0,
                    "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
                },
            },
        )

    def test_network_run_is_unavailable_for_every_receipt_without_network(self) -> None:
        for receipt in ("approval-looking-id", "private-receipt-value"):
            with self.subTest(receipt_kind="opaque"):
                stdout = StringIO()
                stderr = StringIO()
                with (
                    patch.object(socket, "create_connection") as create_connection,
                    patch.object(socket.socket, "connect") as socket_connect,
                    patch.object(urllib.request, "urlopen") as urlopen,
                ):
                    exit_code = main(
                        [
                            "doctor",
                            "network",
                            "run",
                            "--approval-receipt",
                            receipt,
                            "--output",
                            "json",
                        ],
                        stdout=stdout,
                        stderr=stderr,
                    )

                self.assertEqual(exit_code, 30)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(stdout.getvalue().count("\n"), 1)
                self.assertNotIn(receipt, stdout.getvalue())
                self.assertEqual(create_connection.call_count, 0)
                self.assertEqual(socket_connect.call_count, 0)
                self.assertEqual(urlopen.call_count, 0)
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    {
                        "schema_id": "lulu.agent/v1",
                        "schema_revision": 1,
                        "ok": False,
                        "command": "doctor network run",
                        "error": {
                            "code": "CAPABILITY_UNAVAILABLE",
                            "message": "正式 CLI 能力尚未实现",
                            "retryable": False,
                            "human_action": "等待后续正式 CLI 阶段",
                            "details": {
                                "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED"
                            },
                        },
                    },
                )

    def test_argv_secret_options_and_equivalents_are_non_reflectively_rejected(self) -> None:
        secret_value = "PHASE3_PRIVATE_SECRET_VALUE"
        secret_options = (
            "--api-key",
            "--api_key",
            "--apikey",
            "--license-code",
            "--license_code",
            "--license-key",
            "--secret",
            "--token",
            "--access-token",
            "--auth-token",
            "--bearer-token",
            "--credential",
            "--password",
        )

        for option in secret_options:
            with self.subTest(option_kind="secret"):
                stdout = StringIO()
                stderr = StringIO()
                exit_code = main(
                    [
                        "auth",
                        "refresh",
                        option,
                        secret_value,
                        "--output",
                        "json",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(stdout.getvalue().count("\n"), 1)
                self.assertNotIn(option, stdout.getvalue())
                self.assertNotIn(secret_value, stdout.getvalue())
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
                self.assertEqual(
                    payload["error"]["details"],
                    {"reason_code": "SECRET_INPUT_FORBIDDEN"},
                )

        class RaisingInput:
            def read(self, *_args: object, **_kwargs: object) -> str:
                raise AssertionError("stdin/pipe was read")

            def readline(self, *_args: object, **_kwargs: object) -> str:
                raise AssertionError("stdin/pipe was read")

            def isatty(self) -> bool:
                raise AssertionError("TTY state was read")

            def fileno(self) -> int:
                raise AssertionError("PTY descriptor was read")

        sentinels = {
            "LULU_API_KEY": "PHASE3_ENV_API_KEY_SENTINEL",
            "LULU_LICENSE_CODE": "PHASE3_ENV_LICENSE_SENTINEL",
            "LULU_AGENT_SECRET": "PHASE3_AGENT_SENTINEL",
            "LULU_SKILL_SECRET": "PHASE3_SKILL_SENTINEL",
            "LULU_CHAT_SECRET": "PHASE3_CHAT_SENTINEL",
        }
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch.dict(os.environ, sentinels, clear=False),
            patch.object(sys, "stdin", RaisingInput()),
            patch("builtins.open", side_effect=AssertionError("config was opened")) as open_file,
        ):
            exit_code = main(
                ["version", "--output", "json"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(open_file.call_count, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["command"], "version")
        self.assertEqual(payload["result"]["formal_cli_state"], "unavailable")
        for secret in sentinels.values():
            self.assertNotIn(secret, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
