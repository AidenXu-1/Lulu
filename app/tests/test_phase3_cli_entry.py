"""Phase 3 source-entry contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from src.version import __version__, __build__, __channel__


class Phase3CliEntryTests(unittest.TestCase):
    def test_module_entry_emits_one_closed_version_json_object(self):
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "src.agent_cli",
                "version",
                "--output",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": True,
                "command": "version",
                "result": {
                    "product_version": __version__,
                    "build": __build__,
                    "channel": __channel__,
                    "cli_protocol_version": "1.0.0",
                    "formal_cli_state": "unavailable",
                },
            },
        )

    def test_status_is_a_zero_effect_unavailable_state_skeleton(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "src.agent_cli",
                "status",
                "--output",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": True,
                "command": "status",
                "result": {
                    "formal_cli_state": "unavailable",
                    "authorization": {
                        "status": "not_requested",
                        "reason_code": "PHASE3_STATUS_DOES_NOT_READ_AUTH",
                    },
                    "runtime": {
                        "status": "unavailable",
                        "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
                    },
                    "jobs": {
                        "status": "not_requested",
                        "reason_code": "PHASE3_STATUS_DOES_NOT_READ_JOBS",
                    },
                },
            },
        )

    def test_help_lists_only_the_phase3_zero_effect_control_surface(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "src.agent_cli",
                "help",
                "--output",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": True,
                "command": "help",
                "result": {
                    "formal_cli_state": "unavailable",
                    "control_commands": [
                        "help",
                        "version",
                        "capabilities",
                        "status",
                        "doctor",
                        "doctor network prepare",
                    ],
                    "business_dispatch": "unavailable",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
