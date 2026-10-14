"""Phase 8 owner-only launcher-candidate safety tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest


class Phase8LauncherSafetyTests(unittest.TestCase):
    def test_candidate_binds_absolute_cli_and_blocks_overwrite_or_symlink(self) -> None:
        generator_path = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "make_cli_launcher.py"
        )
        try:
            namespace = runpy.run_path(str(generator_path))
            LauncherCandidateError = namespace["LauncherCandidateError"]
            generate_launcher_candidate = namespace["generate_launcher_candidate"]
        except (OSError, KeyError) as exc:
            self.fail(f"Phase 8 launcher candidate generator is missing: {exc}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app_bundle = root / "Lulu.app"
            cli = app_bundle / "Contents" / "MacOS" / "lulu-agent-cli"
            cli.parent.mkdir(parents=True, mode=0o700)
            argv_record = root / "argv-record.json"
            cli.write_text(
                f"#!{sys.executable}\n"
                "import json\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(argv_record)!r}).write_text(\n"
                "    json.dumps(sys.argv[1:], ensure_ascii=False),\n"
                "    encoding='utf-8',\n"
                ")\n",
                encoding="utf-8",
            )
            os.chmod(app_bundle, 0o700)
            os.chmod(app_bundle / "Contents", 0o700)
            os.chmod(cli.parent, 0o700)
            os.chmod(cli, 0o700)
            staging = root / "staging"
            staging.mkdir(mode=0o700)
            candidate = staging / "lulu"

            projection = generate_launcher_candidate(
                app_bundle=app_bundle,
                output_path=candidate,
                expected_bundle_id="com.lulu.transcribe",
                expected_designated_requirement=(
                    'identifier "com.lulu.transcribe" and anchor apple generic'
                ),
                expected_team_id="TESTTEAM01",
            )

            content = candidate.read_text(encoding="utf-8")
            original = candidate.read_bytes()
            self.assertEqual(stat.S_IMODE(candidate.lstat().st_mode), 0o700)
            self.assertEqual(projection["schema_id"], "lulu.launcher-candidate/v1")
            self.assertEqual(projection["canonical_cli"], str(cli))
            self.assertFalse(projection["install_authorized"])
            self.assertEqual(
                projection["authorization_requirement"],
                "REAL_SIGNATURE_AND_INSTALL_REVIEW_REQUIRED",
            )
            canonical_cli = projection["canonical_cli"]
            self.assertEqual(canonical_cli, str(cli))
            self.assertIn(
                f"exec {shlex.quote(canonical_cli)} \"$@\"",
                content,
            )
            self.assertNotIn("command -v", content)
            self.assertNotIn("sudo", content)
            self.assertNotIn("$PATH", content)
            expected_argv = [
                "status",
                "--label",
                "中文 空格",
                "literal$*?[]",
                "--",
                "尾部 参数",
            ]
            completed = subprocess.run(
                [str(candidate), *expected_argv],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                json.loads(argv_record.read_text(encoding="utf-8")),
                expected_argv,
            )
            with self.assertRaises(LauncherCandidateError):
                generate_launcher_candidate(
                    app_bundle=app_bundle,
                    output_path=candidate,
                    expected_bundle_id="com.lulu.transcribe",
                    expected_designated_requirement="forged replacement",
                    expected_team_id="ATTACKER",
                )
            self.assertEqual(candidate.read_bytes(), original)

            symlink_staging = root / "symlink-staging"
            symlink_staging.mkdir(mode=0o700)
            symlink_output = symlink_staging / "lulu"
            symlink_output.symlink_to(candidate)
            with self.assertRaises(LauncherCandidateError):
                generate_launcher_candidate(
                    app_bundle=app_bundle,
                    output_path=symlink_output,
                    expected_bundle_id="com.lulu.transcribe",
                    expected_designated_requirement="forged symlink",
                    expected_team_id="ATTACKER",
                )
            self.assertTrue(symlink_output.is_symlink())
            self.assertEqual(candidate.read_bytes(), original)

            linked_app = root / "Linked.app"
            linked_app.symlink_to(app_bundle, target_is_directory=True)
            blocked_output = staging / "blocked"
            with self.assertRaises(LauncherCandidateError):
                generate_launcher_candidate(
                    app_bundle=linked_app,
                    output_path=blocked_output,
                    expected_bundle_id="com.lulu.transcribe",
                    expected_designated_requirement="linked app",
                    expected_team_id="TESTTEAM01",
                )
            self.assertFalse(blocked_output.exists())


if __name__ == "__main__":
    unittest.main()
