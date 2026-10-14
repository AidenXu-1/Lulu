from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard


class _NoisyCachedLicense:
    def cached_status_projection(self):
        print("private dependency stdout")
        return SimpleNamespace(allowed=True, code="cached_allowed")


class Phase10AgentCliStdioTests(unittest.TestCase):
    def test_local_business_dependency_noise_never_reaches_protocol_streams(self) -> None:
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        with TemporaryDirectory(prefix="lulu-cli-stdio-") as directory:
            source = Path(directory).resolve() / "source.wav"
            source.write_bytes(b"RIFF-stdio")

            def noisy_model(_engine: str) -> bool:
                print("private model stdout")
                return True

            runtime = LocalTranscriptionCliRuntime(
                LocalTranscriptionJobApplication(
                    runtime_verification={
                        "profile": "trusted_test",
                        "verified": True,
                        "signature_kind": "developer_id",
                        "installation_id": "installation-stdio-001",
                    },
                    authorization_guard=UnifiedLicenseAuthGuard(
                        _NoisyCachedLicense()
                    ),
                    model_readiness_probe=noisy_model,
                )
            )
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    ["transcribe", "prepare", str(source), "--output", "json"],
                    product_identity={"adapter_id": "agent_cli"},
                    local_transcription_runtime=runtime,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(stdout.getvalue().count("\n"), 1)
            self.assertTrue(json.loads(stdout.getvalue())["ok"])
            self.assertNotIn("private", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
