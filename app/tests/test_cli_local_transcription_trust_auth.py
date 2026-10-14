from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard


class _CachedLicense:
    def __init__(self, *, allowed: bool, code: str) -> None:
        self.status = SimpleNamespace(allowed=allowed, code=code)
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return self.status


class CliLocalTranscriptionTrustAuthTests(unittest.TestCase):
    def _runtime(self, verification, license_service, model_calls, *, ready=True):
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import (
            LocalTranscriptionJobApplication,
        )

        return LocalTranscriptionCliRuntime(
            LocalTranscriptionJobApplication(
                runtime_verification=verification,
                authorization_guard=UnifiedLicenseAuthGuard(license_service),
                model_readiness_probe=(
                    lambda engine: model_calls.append(engine) or ready
                ),
            )
        )

    def _invoke(self, argv, runtime):
        stdout = StringIO()
        stderr = StringIO()
        code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            product_identity={"adapter_id": "agent_cli"},
            local_transcription_runtime=runtime,
        )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_fail_closed_order_prevents_later_boundary_calls(self) -> None:
        with TemporaryDirectory(prefix="lulu-cli-trust-auth-") as directory:
            root = Path(directory).resolve()
            source = root / "source.wav"
            source.write_bytes(b"RIFF-trust-auth-order")
            verified = {
                "profile": "trusted_test",
                "verified": True,
                "signature_kind": "developer_id",
                "installation_id": "installation-001",
            }

            for label, verification in (
                (
                    "unverified",
                    {**verified, "verified": False},
                ),
                (
                    "ad_hoc",
                    {**verified, "signature_kind": "ad_hoc"},
                ),
                (
                    "wrong_profile",
                    {**verified, "profile": "external_candidate"},
                ),
            ):
                with self.subTest(label=label):
                    license_service = _CachedLicense(
                        allowed=True, code="cached_allowed"
                    )
                    model_calls: list[str] = []
                    code, envelope, stderr = self._invoke(
                        ["transcribe", "prepare", str(source), "--output", "json"],
                        self._runtime(verification, license_service, model_calls),
                    )
                    self.assertEqual((code, stderr), (30, ""))
                    self.assertEqual(envelope["error"]["code"], "CAPABILITY_UNAVAILABLE")
                    self.assertEqual(
                        envelope["error"]["details"]["reason_code"],
                        "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
                    )
                    self.assertEqual(license_service.calls, 0)
                    self.assertEqual(model_calls, [])

            denied = _CachedLicense(allowed=False, code="cached_denied")
            denied_models: list[str] = []
            code, envelope, _stderr = self._invoke(
                ["transcribe", "prepare", str(source), "--output", "json"],
                self._runtime(verified, denied, denied_models),
            )
            self.assertEqual(code, 10)
            self.assertEqual(envelope["error"]["code"], "AUTH_REQUIRED")
            self.assertEqual(denied.calls, 1)
            self.assertEqual(denied_models, [])

            missing_license = _CachedLicense(allowed=True, code="cached_allowed")
            missing_models: list[str] = []
            code, envelope, _stderr = self._invoke(
                [
                    "transcribe",
                    "prepare",
                    str(root / "missing.wav"),
                    "--output",
                    "json",
                ],
                self._runtime(verified, missing_license, missing_models),
            )
            self.assertEqual(code, 20)
            self.assertEqual(envelope["error"]["code"], "NOT_FOUND")
            self.assertEqual(missing_license.calls, 1)
            self.assertEqual(missing_models, [])

            unready_license = _CachedLicense(allowed=True, code="cached_allowed")
            unready_models: list[str] = []
            code, envelope, _stderr = self._invoke(
                ["transcribe", "prepare", str(source), "--output", "json"],
                self._runtime(
                    verified,
                    unready_license,
                    unready_models,
                    ready=False,
                ),
            )
            self.assertEqual(code, 30)
            self.assertEqual(envelope["error"]["code"], "MODEL_NOT_READY")
            self.assertEqual(unready_license.calls, 1)
            self.assertEqual(unready_models, ["local_sensevoice"])

    def test_verified_runtime_discovers_all_local_models_without_cloud_escalation(self) -> None:
        verified = {
            "profile": "trusted_test",
            "verified": True,
            "signature_kind": "developer_id",
            "installation_id": "installation-001",
        }
        license_service = _CachedLicense(allowed=True, code="cached_allowed")
        runtime = self._runtime(verified, license_service, [])

        code, envelope, stderr = self._invoke(
            ["capabilities", "--output", "json"], runtime
        )

        self.assertEqual((code, stderr), (0, ""))
        result = envelope["result"]
        from src.core.agent_workspace.contracts import CAPABILITIES as WORKSPACE_CAPABILITIES
        from src.core.link_assets import CAPABILITIES as EXTRACTION_CAPABILITIES
        expected = {
            "TR-01", "TR-02", "TR-03", "TR-11", "TR-12",
            "JOB-01", "JOB-02", "JOB-03", "JOB-06", "LIB-01", "LIB-02", "COPY-07",
        } | WORKSPACE_CAPABILITIES
        self.assertEqual(result["capability_count"], 139 + len(WORKSPACE_CAPABILITIES) + len(EXTRACTION_CAPABILITIES))
        self.assertEqual(
            result["state_counts"],
            {"available": len(expected), "unavailable": 120 + len(EXTRACTION_CAPABILITIES), "gui_only": 7},
        )
        activated = {
            item["capability_id"]
            for item in result["capabilities"]
            if item["state"] == "available"
        }
        self.assertEqual(
            activated,
            expected,
        )
        rows = {row['capability_id']: row for row in result['capabilities']}
        self.assertEqual(rows['TR-03']['engines'], ['local_sensevoice', 'local_qwen3', 'local_whisper_mlx'])
        self.assertEqual(rows['TR-04']['state'], 'unavailable')
        self.assertEqual(rows['TR-05']['state'], 'unavailable')
        self.assertTrue(all(
            item["executable"] is (item["capability_id"] in activated)
            for item in result["capabilities"]
        ))
        self.assertEqual(license_service.calls, 0)


if __name__ == "__main__":
    unittest.main()
