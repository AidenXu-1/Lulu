"""Open product policy with isolated OS storage, no network or real credentials."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


from src.core.auth_guard import OpenProductAccessGuard
from src.core.transcription_application import (
    LocalFileTranscriptionApplicationService, LocalFileTranscriptionError,
)
from src.core.transcription_contracts import LocalFileTranscriptionRequestV1
from src.core.transcription_job_application import (
    LocalTranscriptionJobApplication, LocalTranscriptionJobError,
)
from tests.test_transcription_application_service import (
    _write_phase1a_fixture, _deterministic_pipeline_runner,
)
from test_installed_runtime_bootstrap_public import InstalledRuntimeBootstrapPublicTests
from test_cli_trusted_runtime_factory import _cache_json
from tests.test_cli_local_synthesis import LocalSynthesisTests


class OpenProductPolicyTests(unittest.TestCase):
    def test_retired_capabilities_cannot_be_reactivated(self):
        from src.agent_cli.catalog import capability_discovery, RETIRED_LICENSE_CAPABILITIES
        result = capability_discovery(activated_capability_ids=RETIRED_LICENSE_CAPABILITIES)
        retired = [item for item in result["capabilities"]
                   if item["capability_id"] in RETIRED_LICENSE_CAPABILITIES]
        self.assertEqual(len(retired), 6)
        for item in retired:
            self.assertEqual(item["state"], "unavailable")
            self.assertEqual(item["reason_code"], "LICENSE_FEATURE_RETIRED")
            self.assertFalse(item["executable"])

    def test_local_gui_transcription_runs_without_license_or_network(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                "src.core.license.external_service": None,
                "src.core.cli_runtime.license_projection": None,
            }))
            for target in (
                "src.core.license.store.StrictKeyringStore",
                "socket.create_connection", "urllib.request.urlopen",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            source = _write_phase1a_fixture(directory)
            service = LocalFileTranscriptionApplicationService(
                OpenProductAccessGuard(), pipeline_runner=_deterministic_pipeline_runner,
                model_readiness_probe=lambda _: True,
            )
            self.assertEqual(service.transcribe(LocalFileTranscriptionRequestV1(source)).text, "测试音频。")

    def test_none_foreign_and_subclass_policies_still_fail_closed(self):
        class Foreign:
            def assert_local_file_transcription_allowed(self, _request):
                pass
        class Forged(OpenProductAccessGuard):
            pass
        for guard in (None, Foreign(), Forged()):
            with self.subTest(guard=type(guard).__name__):
                service = LocalFileTranscriptionApplicationService(guard)
                with self.assertRaises(LocalFileTranscriptionError) as error:
                    service.transcribe(LocalFileTranscriptionRequestV1("/missing.wav"))
                self.assertEqual(error.exception.failure.code, "AUTH_REQUIRED")

    def test_open_access_does_not_bypass_installation_trust(self):
        application = LocalTranscriptionJobApplication(
            runtime_verification={"profile": "trusted_test", "verified": False},
            authorization_guard=OpenProductAccessGuard(),
            model_readiness_probe=lambda _: True,
        )
        for operation in (
            lambda: application.assert_job_control_allowed("JOB-01"),
            lambda: application.assert_local_synthesis_allowed("TTS-01"),
        ):
            with self.assertRaises(LocalTranscriptionJobError) as error:
                operation()
            self.assertEqual(error.exception.reason_code, "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE")

    def test_file_and_model_checks_survive_open_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(_write_phase1a_fixture(directory))
            link = source.with_name("link.wav")
            link.symlink_to(source)
            service = LocalFileTranscriptionApplicationService(
                OpenProductAccessGuard(), model_readiness_probe=lambda _: False,
            )
            for path, code in ((source, "MODEL_NOT_READY"), (link, "UNSAFE_PATH"),
                               (source.with_name("missing.wav"), "NOT_FOUND")):
                with self.subTest(code=code), self.assertRaises(LocalFileTranscriptionError) as error:
                    service.transcribe(LocalFileTranscriptionRequestV1(str(path)))
                self.assertEqual(error.exception.failure.code, code)


class RetiredLicenseInstalledRuntimeTests(unittest.TestCase):
    """Real installer/runtime algorithms, fake OS home/services/keys only."""
    setUp = InstalledRuntimeBootstrapPublicTests.setUp
    command = InstalledRuntimeBootstrapPublicTests.command
    install_product = InstalledRuntimeBootstrapPublicTests.install_product
    _construct_default_runtime = InstalledRuntimeBootstrapPublicTests._construct_default_runtime

    def _exercise(self, state):
        raw = json.loads(_cache_json(datetime.now()))
        if state in ("expired", "grace_exhausted"):
            raw["grace_until"] = (datetime.now() - timedelta(days=9)).isoformat()
        if state == "revoked":
            raw["entitlements"] = []
        self.legacy_license_fixture = {} if state == "never_activated" else {
            "license_cache_v1": "broken-json" if state == "corrupt" else json.dumps(raw),
            "license_device_salt_v1": "fake-salt-preserve",
        }
        if state == "deactivation_pending":
            self.legacy_license_fixture["license_pending_deactivation_v1"] = "fake-pending-preserve"
        def guarded(method):
            def call(key, *args, **kwargs):
                if key.startswith("license_"):
                    raise AssertionError("Retired credential accessed: " + key)
                return method(key, *args, **kwargs)
            return call
        for method in ("get_text", "write_new", "delete_text"):
            setattr(self.keys, method, guarded(getattr(self.keys, method)))
        # Also execute a new CLI job and reopen historical results twice.
        with patch("socket.create_connection", side_effect=AssertionError("offline")) as connect, patch(
            "urllib.request.urlopen", side_effect=AssertionError("offline")
        ) as fetch:
            InstalledRuntimeBootstrapPublicTests.test_existing_results_default_bootstrap_cli_run_read_and_reopen(self)
        connect.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(
            {key: value for key, value in self.keys.values.items() if key.startswith("license_")},
            self.legacy_license_fixture,
        )

    def test_never_activated(self): self._exercise("never_activated")
    def test_valid(self): self._exercise("valid")
    def test_expired(self): self._exercise("expired")
    def test_revoked(self): self._exercise("revoked")
    def test_grace_exhausted(self): self._exercise("grace_exhausted")
    def test_deactivation_pending(self): self._exercise("deactivation_pending")
    def test_corrupt(self): self._exercise("corrupt")


class OpenProductSynthesisTests(unittest.TestCase):
    setUp = LocalSynthesisTests.setUp
    wav = staticmethod(LocalSynthesisTests.wav)
    invoke = LocalSynthesisTests.invoke
    start = LocalSynthesisTests.start
    finish = LocalSynthesisTests.finish

    def test_cli_synthesis_publishes_audio_without_license_projection(self):
        self.application._authorization_guard = OpenProductAccessGuard()
        with patch.object(self.license, "cached_status_projection",
                          side_effect=AssertionError("retired license read")) as projection:
            LocalSynthesisTests.test_async_job_verified_audio_and_gui_separation(self)
        projection.assert_not_called()
