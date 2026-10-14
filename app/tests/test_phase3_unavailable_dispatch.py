"""Phase 3 unavailable-dispatch contract tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class Phase3UnavailableDispatchTests(unittest.TestCase):
    def test_retired_auth_command_has_a_permanent_non_retryable_rejection(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "src.agent_cli",
                "auth",
                "refresh",
                "--output",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 30, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": False,
                "command": "auth refresh",
                "error": {
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "授权码功能已退役；新版 Lulu 无需激活。",
                    "retryable": False,
                    "human_action": "直接使用新版 Lulu，无需提供授权码",
                    "details": {
                        "capability_id": "AUTH-03",
                        "reason_code": "LICENSE_FEATURE_RETIRED",
                    },
                },
            },
        )

    def test_unknown_command_is_one_non_reflective_json_error(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "src.agent_cli",
                "private-unknown-command",
                "--output",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertNotIn("private-unknown-command", completed.stdout)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": False,
                "command": "unknown",
                "error": {
                    "code": "UNKNOWN_COMMAND",
                    "message": "命令不在 Phase 3 闭合 grammar 中",
                    "retryable": False,
                    "human_action": "使用 help 或 capabilities 查看当前控制面",
                    "details": {},
                },
            },
        )

    def test_registered_command_without_json_output_has_no_traceback(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(workspace / "app")

        completed = subprocess.run(
            [sys.executable, "-B", "-m", "src.agent_cli", "auth", "refresh"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("Traceback", completed.stdout)
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "schema_id": "lulu.agent/v1",
                "schema_revision": 1,
                "ok": False,
                "command": "invalid",
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "参数不符合 Phase 3 闭合 grammar",
                    "retryable": False,
                    "human_action": "使用 help 或 capabilities 查看当前控制面",
                    "details": {"reason_code": "JSON_OUTPUT_REQUIRED"},
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
