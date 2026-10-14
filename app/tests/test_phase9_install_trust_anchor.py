"""Phase 9 canonical App/CLI trust-anchor verification tests."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import runpy
import subprocess
from types import SimpleNamespace
import tempfile
import unittest


_PRODUCT_MANIFEST = (
    b'{"adapters":{"agent_cli":{"executable":"Contents/MacOS/lulu-agent-cli"}},'
    b'"cli_protocol_version":"1.0.0",'
    b'"installation_identity_scope":"shared_lulu_installation",'
    b'"product_build":"20260815.3","product_version":"0.3.1",'
    b'"schema_id":"lulu.agent-product-manifest/v1","schema_revision":1}\n'
)
_PRODUCT_MANIFEST_SHA256 = "d5c2240eae8015cb19f986420049c2cbc6b4b45b1cbfc0cf1e1328497a5f961f"
_CANONICAL_CLI_SHA256 = "ce47e6b495e0787e435e82e52d3facfcf3fe57e116027dad22d0791e15db24a3"
_BUILD_MANIFEST_SHA256 = "10c397b0264077aaf8ede44f994ef1b3cbf191c8a0fc023c3652090120fe952a"
_REQUIREMENT = (
    'identifier "com.lulu.transcribe" and anchor apple generic and '
    'certificate leaf[subject.OU] = TESTTEAM01'
)


class _FakeCodesignRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> SimpleNamespace:
        self.calls.append(list(argv))
        if argv[1:4] == ["--verify", "--deep", "--strict"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["-dr", "-"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=f"Executable={argv[-1]}\ndesignated => {_REQUIREMENT}\n",
            )
        if argv[1:3] == ["--display", "--verbose=4"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(
                    "Identifier=com.lulu.transcribe\n"
                    "TeamIdentifier=TESTTEAM01\n"
                ),
            )
        raise AssertionError(f"unexpected runner call: {argv!r}")


def _write(path: Path, content: bytes, mode: int) -> None:
    path.write_bytes(content)
    os.chmod(path, mode)


class Phase9InstallTrustAnchorTests(unittest.TestCase):
    @staticmethod
    def _namespace() -> dict[str, object]:
        app_root = Path(__file__).resolve().parents[1]
        return runpy.run_path(
            str(
                app_root
                / "skills"
                / "lulu-agent"
                / "scripts"
                / "verify_lulu_install.py"
            )
        )

    def test_source_cli_exposes_no_caller_supplied_trust_pins_and_stays_unverified(self) -> None:
        namespace = self._namespace()
        main = namespace["main"]
        for argv in (
            [],
            ["--app", "/Applications/Lulu.app"],
            ["--bundle-id", "com.lulu.transcribe"],
            ["--team-id", "ATTACKER01"],
            ["--requirement", "attacker requirement"],
            ["--manifest-sha256", "0" * 64],
            ["--build-manifest-json", "{}"],
        ):
            with self.subTest(argv=argv):
                stdout = StringIO()
                stderr = StringIO()
                try:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        exit_code = main(argv)
                except SystemExit as exc:
                    self.fail(
                        f"production verifier still parses caller trust pins: {exc}"
                    )
                self.assertEqual(exit_code, 60)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    {
                        "schema_id": "lulu.install-verification/v1",
                        "verified": False,
                        "reason_code": "TRUST_EVIDENCE_UNAVAILABLE",
                    },
                )

    def test_direct_skill_verifier_fails_closed_without_traceback_on_macos_python39(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (
            app_root
            / "skills"
            / "lulu-agent"
            / "scripts"
            / "verify_lulu_install.py"
        )
        self.assertEqual(script.read_bytes().splitlines()[0], b"#!/usr/bin/env python3")

        version = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(version.stdout.strip(), "3.9")

        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HOME": directory,
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": directory,
            }
            completed = subprocess.run(
                [str(script)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(completed.returncode, 60)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("TypeError", completed.stderr)
        self.assertEqual(
            completed.stdout,
            '{"reason_code":"TRUST_EVIDENCE_UNAVAILABLE",'
            '"schema_id":"lulu.install-verification/v1","verified":false}\n',
        )

    def test_sealed_policy_verifies_signature_first_and_binds_canonical_cli_hash(self) -> None:
        namespace = self._namespace()
        self.assertIn(
            "_SealedInstallPolicy",
            namespace,
            "sealed install policy DTO is missing",
        )
        self.assertIn(
            "_verify_lulu_install_with_sealed_policy",
            namespace,
            "sealed-policy verifier is missing",
        )
        SealedInstallPolicy = namespace["_SealedInstallPolicy"]
        verify_lulu_install = namespace["_verify_lulu_install_with_sealed_policy"]
        TrustAnchorError = namespace["TrustAnchorError"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / "Lulu.app"
            contents = app / "Contents"
            macos = contents / "MacOS"
            resources = contents / "Resources"
            macos.mkdir(parents=True, mode=0o700)
            resources.mkdir(mode=0o700)
            for path in (app, contents, macos, resources):
                os.chmod(path, 0o700)
            cli = macos / "lulu-agent-cli"
            _write(cli, b"fake frozen CLI", 0o700)
            info = contents / "Info.plist"
            _write(
                info,
                plistlib.dumps({"CFBundleIdentifier": "com.lulu.transcribe"}),
                0o600,
            )
            _write(
                resources / "agent_product_manifest.json",
                _PRODUCT_MANIFEST,
                0o600,
            )
            build_profile = {
                "schema": 1,
                "profile": "external_candidate",
                "version": "0.3.1",
                "build": "20260815.3",
                "source_commit": "1" * 40,
                "source_tree": "2" * 40,
                "product_source_sha256": "3" * 64,
                "bundle_id": "com.lulu.transcribe",
                "canonical_cli_sha256": _CANONICAL_CLI_SHA256,
            }
            _write(
                resources / "build_profile.json",
                (json.dumps(build_profile, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                0o600,
            )
            bin_root = root / "bin"
            bin_root.mkdir(mode=0o700)
            launcher = bin_root / "lulu"
            launcher.symlink_to(cli)
            runner = _FakeCodesignRunner()
            policy = SealedInstallPolicy(
                allowed_app_bundles=(app,),
                expected_bundle_id="com.lulu.transcribe",
                expected_designated_requirement=_REQUIREMENT,
                expected_team_id="TESTTEAM01",
                expected_product_manifest_sha256=_PRODUCT_MANIFEST_SHA256,
                expected_build_manifest_sha256=_BUILD_MANIFEST_SHA256,
            )
            kwargs = {
                "app_bundle": app,
                "sealed_policy": policy,
                "path_candidate": launcher,
                "runner": runner,
            }

            try:
                result = verify_lulu_install(**kwargs)
            except TrustAnchorError as exc:
                self.fail(f"valid fake signed installation was not verified: {exc}")

            self.assertEqual(
                result,
                {
                    "schema_id": "lulu.install-verification/v1",
                    "verified": True,
                    "canonical_app": str(app),
                    "canonical_cli": str(cli),
                    "convenience_path": str(launcher),
                    "convenience_path_equivalent": True,
                    "bundle_id": "com.lulu.transcribe",
                    "designated_requirement": _REQUIREMENT,
                    "team_id": "TESTTEAM01",
                    "signed_manifest_sha256": _PRODUCT_MANIFEST_SHA256,
                    "product_version": "0.3.1",
                    "product_build": "20260815.3",
                    "source_commit": "1" * 40,
                    "source_tree": "2" * 40,
                    "installation_identity_scope": "shared_lulu_installation",
                    "cli_protocol_version": "1.0.0",
                },
            )
            self.assertEqual(
                runner.calls,
                [
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                    ["/usr/bin/codesign", "-dr", "-", str(app)],
                    ["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                ],
            )

            stable_calls = list(runner.calls)
            linked_app = root / "Linked.app"
            linked_app.symlink_to(app, target_is_directory=True)
            forged = dict(kwargs)
            forged["app_bundle"] = linked_app
            forged["path_candidate"] = None
            with self.assertRaises(TrustAnchorError) as raised:
                verify_lulu_install(**forged)
            self.assertEqual(raised.exception.reason_code, "APP_SYMLINK_FORBIDDEN")
            self.assertEqual(runner.calls, stable_calls)

            _write(cli, b"tampered CLI", 0o700)
            wrong_digest = dict(kwargs)
            wrong_digest["path_candidate"] = None
            with self.assertRaises(TrustAnchorError) as raised:
                verify_lulu_install(**wrong_digest)
            self.assertEqual(
                raised.exception.reason_code,
                "CANONICAL_CLI_HASH_MISMATCH",
            )
            self.assertEqual(
                runner.calls[len(stable_calls) :],
                [
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                    ["/usr/bin/codesign", "-dr", "-", str(app)],
                    ["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
                ],
            )


if __name__ == "__main__":
    unittest.main()
