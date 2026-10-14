from __future__ import annotations

from io import StringIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard


class _FreshCachedLicense:
    def __init__(self) -> None:
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return SimpleNamespace(allowed=True, code="cached_allowed")


class CliLocalTranscriptionPrepareTests(unittest.TestCase):
    def test_prepare_is_verified_authorized_ready_and_zero_write(self) -> None:
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        with TemporaryDirectory(prefix="lulu-cli-prepare-") as directory:
            root = Path(directory).resolve()
            source = root / "sample.wav"
            payload = b"RIFF-local-sensevoice-fixture"
            source.write_bytes(payload)
            before = {
                path.relative_to(root): (path.stat().st_mode, path.read_bytes())
                for path in root.iterdir()
                if path.is_file()
            }
            license_service = _FreshCachedLicense()
            model_calls: list[str] = []
            application = LocalTranscriptionJobApplication(
                runtime_verification={
                    "profile": "trusted_test",
                    "verified": True,
                    "signature_kind": "developer_id",
                    "installation_id": "installation-test-001",
                },
                authorization_guard=UnifiedLicenseAuthGuard(license_service),
                model_readiness_probe=lambda engine: model_calls.append(engine) or True,
            )
            stdout = StringIO()
            stderr = StringIO()

            exit_code = main(
                ["transcribe", "prepare", str(source), "--output", "json"],
                stdout=stdout,
                stderr=stderr,
                product_identity={"adapter_id": "agent_cli"},
                local_transcription_runtime=LocalTranscriptionCliRuntime(application),
            )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            envelope = json.loads(stdout.getvalue())
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "transcribe.prepare")
            self.assertEqual(envelope["result"]["status"], "ready")
            self.assertEqual(envelope["result"]["source_summary"], {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
            self.assertEqual(envelope["result"]["engine"], "local_sensevoice")
            self.assertEqual(license_service.calls, 1)
            self.assertEqual(model_calls, ["local_sensevoice"])
            after = {
                path.relative_to(root): (path.stat().st_mode, path.read_bytes())
                for path in root.iterdir()
                if path.is_file()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
