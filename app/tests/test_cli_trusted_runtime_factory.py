from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.license.models import FULL_ACCESS, LicenseCache
from src.core.models import Segment, Transcript


class FakeKeychainBoundary:
    def __init__(
        self,
        values: dict[str, str],
        *,
        fail_reads: bool = False,
        fail_after_reads: int | None = None,
        fail_write_at: int | None = None,
    ) -> None:
        self.values = dict(values)
        self.fail_reads = fail_reads
        self.fail_after_reads = fail_after_reads
        self.fail_write_at = fail_write_at
        self.reads = 0
        self.write_attempts = 0
        self.mutations = 0
        self.write_order: list[str] = []
        self.read_keys: list[str] = []
        self.delete_order: list[str] = []

    def get_text(self, key: str) -> str | None:
        if self.fail_reads or (
            self.fail_after_reads is not None
            and self.reads >= self.fail_after_reads
        ):
            raise RuntimeError("fake keychain unavailable")
        self.reads += 1
        self.read_keys.append(key)
        return self.values.get(key)

    def set_text(self, key: str, value: str) -> None:
        self.write_attempts += 1
        if self.fail_write_at == self.write_attempts:
            raise RuntimeError("fake keychain write unavailable")
        self.mutations += 1
        self.write_order.append(key)
        self.values[key] = value

    def write_new(self, key: str, value: str) -> None:
        self.write_attempts += 1
        if self.fail_write_at == self.write_attempts:
            raise RuntimeError("fake keychain write unavailable")
        if key in self.values:
            raise RuntimeError("fake keychain account exists")
        self.mutations += 1
        self.write_order.append(key)
        self.values[key] = value

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        if key not in self.values and not missing_ok:
            raise RuntimeError("fake keychain account missing")
        self.delete_order.append(key)
        self.values.pop(key, None)


class FakeSignatureVerifier:
    def __init__(self, evidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[Path, Path]] = []

    def verify(self, *, app_bundle: Path, cli_path: Path):
        self.calls.append((app_bundle, cli_path))
        return self.evidence


def _cache_json(now: datetime) -> str:
    cache = LicenseCache(
        schema_version=1,
        identity_id="identity-test",
        identity_kind="license",
        license_id="license-secret-must-not-escape",
        device_id="device-secret-must-not-escape",
        device_hash="device-hash-secret-must-not-escape",
        entitlements=(FULL_ACCESS,),
        lease_token="lease-token-secret-must-not-escape",
        last_verified_at=now - timedelta(hours=1),
        next_check_at=now + timedelta(days=1),
        grace_until=now + timedelta(days=7),
        last_local_seen_at=now - timedelta(minutes=1),
    )
    return json.dumps(cache.to_json(), ensure_ascii=False, separators=(",", ":"))


class TrustedRuntimeFactoryTests(unittest.TestCase):
    def test_versioned_runtime_derives_app_and_cli_only_from_authority(self) -> None:
        from src.core.cli_runtime.factory import _build_verified_trusted_test_runtime

        parameters = inspect.signature(
            _build_verified_trusted_test_runtime
        ).parameters
        self.assertNotIn("resource_root", parameters)
        self.assertNotIn("cli_path", parameters)
        self.assertTrue({"locator_receipt", "plan_receipt"}.issubset(parameters))

    def test_macos_verifier_accepts_real_ad_hoc_designated_comment_format(self) -> None:
        from src.core.cli_runtime.macos_signature import MacOSCodeSignatureVerifier

        outputs = iter(
            (
                "",
                "",
                "Signature=adhoc\nCDHash=" + "c" * 40 + "\nTeamIdentifier=not set\n",
                'Executable=/Applications/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli\n'
                '# designated => cdhash H"00beb4bbe463ce428b79a02a88a27afcacade90d"\n',
            )
        )
        verifier = MacOSCodeSignatureVerifier()
        verifier._run = lambda *_arguments: next(outputs)

        evidence = verifier.verify(
            app_bundle=Path("/Applications/Lulu Trusted Test.app"),
            cli_path=Path(
                "/Applications/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli"
            ),
        )

        self.assertEqual(evidence.signature_kind, "ad_hoc")
        self.assertEqual(
            evidence.designated_requirement,
            'cdhash H"00beb4bbe463ce428b79a02a88a27afcacade90d"',
        )

    def _fixture(self, root: Path):
        from src.core.cli_runtime.contracts import CodeSignatureEvidenceV1

        app_bundle = root / "Lulu Trusted Test.app"
        resources = app_bundle / "Contents/Resources"
        macos = app_bundle / "Contents/MacOS"
        resources.mkdir(parents=True)
        macos.mkdir()
        cli_path = macos / "lulu-agent-cli"
        gui_path = macos / "Lulu-Trusted-Test"
        cli_path.write_bytes(b"canonical trusted-test cli\n")
        gui_path.write_bytes(b"canonical trusted-test gui\n")
        cli_path.chmod(0o755)
        gui_path.chmod(0o755)
        now = datetime(2026, 8, 21, 10, 0, 0)
        source_commit = "a" * 40
        source_tree = "b" * 40
        bundle_id = "com.lulu.transcribe.trusted-test"
        with (app_bundle / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleExecutable": "Lulu-Trusted-Test",
                    "CFBundlePackageType": "APPL",
                },
                handle,
            )
        installation_id = "install-local-trusted-test-001"
        cli_sha256 = hashlib.sha256(cli_path.read_bytes()).hexdigest()
        signature = CodeSignatureEvidenceV1(
            signature_kind="ad_hoc",
            certificate_sha256="",
            cdhash="c" * 40,
            designated_requirement="identifier com.lulu.transcribe.trusted-test",
            team_id="",
        )
        ticket = {
            "schema_id": "lulu.agent-cli-installation-ticket/v2",
            "schema_revision": 2,
            "trust_profile": "local_trusted_test",
            "bundle_id": bundle_id,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "canonical_cli_sha256": cli_sha256,
            "installation_id": installation_id,
            "signature_kind": signature.signature_kind,
            "certificate_sha256": signature.certificate_sha256,
            "cdhash": signature.cdhash,
            "designated_requirement": signature.designated_requirement,
            "team_id": signature.team_id,
            "approval_issuer_source": "trusted_gui_human_action",
            "approval_nonce": "fixture-approval-nonce-00000001",
            "approval_issued_at": "2026-08-21T09:59:00.000000Z",
            "approval_expires_at": "2026-08-21T10:01:00.000000Z",
            "approval_receipt_sha256": "9" * 64,
        }
        keychain = FakeKeychainBoundary(
            {
                "lulu_agent_cli_installation_ticket_v1": json.dumps(
                    ticket, sort_keys=True, separators=(",", ":")
                ),
                "license_cache_v1": _cache_json(now),
                f"lulu_agent_cli_job_hmac_key_v1:{installation_id}": base64.b64encode(
                    b"t" * 32
                ).decode("ascii"),
                f"lulu_agent_cli_job_anchor_v1:{installation_id}": json.dumps(
                    {
                        "epoch": "d" * 32,
                        "sequence": 0,
                        "head_hmac": "0" * 64,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        product_identity = {
            "shared": {
                "profile": "trusted_test",
                "bundle_id": bundle_id,
                "product_version": "0.3.2",
                "product_build": "20260823.1",
                "source_commit": source_commit,
                "source_tree": source_tree,
            },
            "adapter_id": "agent_cli",
        }
        application_support = root / "Application Support"
        application_support.mkdir(mode=0o700)
        os.chmod(application_support, 0o700)
        return SimpleNamespace(
            app_bundle=app_bundle,
            resources=resources,
            cli_path=cli_path,
            now=now,
            signature=signature,
            keychain=keychain,
            product_identity=product_identity,
            application_support=application_support,
            installation_id=installation_id,
        )

    def test_user_ticket_and_shared_gui_cache_enable_local_trusted_test(self) -> None:
        from src.core.cli_runtime.factory import _build_legacy_trusted_test_runtime

        with TemporaryDirectory(prefix="lulu-cli-trusted-factory-") as directory:
            fixture = self._fixture(Path(directory).resolve())
            runtime = _build_legacy_trusted_test_runtime(
                product_identity=fixture.product_identity,
                resource_root=fixture.resources,
                cli_path=fixture.cli_path,
                application_support_root=fixture.application_support,
                keychain=fixture.keychain,
                signature_verifier=FakeSignatureVerifier(fixture.signature),
                now=lambda: fixture.now,
                link_cover_workflow=object(),
            )

            self.assertIn("TR-02", runtime.activated_capability_ids)
            self.assertTrue(
                {"TR-05", "TR-06", "TR-07", "TR-08"}.issubset(
                    runtime.activated_capability_ids
                )
            )
            self.assertIn("JOB-06", runtime.activated_capability_ids)
            self.assertIsNone(runtime._link_host_broker)
            projection = json.dumps(
                sorted(runtime.activated_capability_ids), ensure_ascii=False
            )
            for secret in ("license-secret", "device-secret", "lease-token-secret"):
                self.assertNotIn(secret, projection)

    def test_versioned_runtime_reads_only_authoritative_v3_and_enables_prepare(self) -> None:
        from io import StringIO

        from src.agent_cli.main import main as cli_main
        from src.core.cli_runtime.factory import _build_verified_trusted_test_runtime
        from src.core.cli_runtime.installation_ticket import (
            _application_support_root_from_authority,
        )
        from src.core.cli_runtime.keychain import (
            provision_versioned_installation_credentials,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _patched_test_authority_validation,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-cli-v3-runtime-") as directory:
            root = Path(directory).resolve()
            system_home = root / "system-home"
            library = system_home / "Library"
            library.mkdir(parents=True, mode=0o700)
            os.chmod(system_home, 0o700)
            os.chmod(library, 0o700)
            fixture = self._fixture(library)
            attacker_home = root / "attacker-home"
            attacker_home.mkdir(mode=0o700)
            attacker_before = tuple(attacker_home.rglob("*"))
            candidate = {
                "bundle_id": fixture.product_identity["shared"]["bundle_id"],
                "product_version": "0.3.2",
                "build": "20260823.1",
                "source_commit": fixture.product_identity["shared"]["source_commit"],
                "source_tree": fixture.product_identity["shared"]["source_tree"],
                "app_content_tree_sha256": "c" * 64,
                "app_profile_tree_sha256": "d" * 64,
                "canonical_cli_sha256": hashlib.sha256(
                    fixture.cli_path.read_bytes()
                ).hexdigest(),
                "signature": {
                    "signature_kind": "ad_hoc",
                    "certificate_sha256": "",
                    "cdhash": fixture.signature.cdhash,
                    "designated_requirement": f'cdhash H"{fixture.signature.cdhash}"',
                    "team_id": "",
                },
            }
            signature = replace(
                fixture.signature,
                designated_requirement=candidate["signature"]["designated_requirement"],
            )

            locator, plan = _authority(
                fixture.application_support,
                "runtime-fixture-nonce",
                candidate=candidate,
                installation_target=fixture.resources.parents[1],
            )
            with _patched_test_authority_validation():
                provision_versioned_installation_credentials(
                    post_build_receipt=locator,
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=fixture.keychain,
                    hmac_key_factory=lambda: b"v" * 32,
                    epoch_factory=lambda: "e" * 32,
                )
            fixture.keychain.read_keys.clear()
            with (
                patch.dict(os.environ, {"HOME": str(attacker_home)}),
                patch(
                    "pwd.getpwuid",
                    return_value=SimpleNamespace(
                        pw_dir=str(system_home), pw_uid=os.getuid()
                    ),
                ),
                _patched_test_authority_validation(),
            ):
                authority = _validate_test_authority(
                    locator_receipt=locator,
                    plan_receipt=plan,
                )
                application_support = _application_support_root_from_authority(
                    authority
                )
                runtime = _build_verified_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    application_support_root=application_support,
                    keychain=fixture.keychain,
                    signature_verifier=FakeSignatureVerifier(signature),
                    locator_receipt=locator,
                    plan_receipt=plan,
                    now=lambda: fixture.now,
                    model_readiness_probe=lambda _engine: True,
                    link_cover_workflow=object(),
                )
            self.assertEqual(application_support, fixture.application_support)
            self.assertEqual(tuple(attacker_home.rglob("*")), attacker_before)
            verification = runtime._application._runtime_verification
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            self.assertEqual(
                verification["target_binding_sha256"],
                authority.target_binding_sha256,
            )
            self.assertEqual(
                verification["installation_target"],
                authority.installation_target,
            )
            self.assertEqual(verification["target_role"], "trusted_test")

            source = Path(directory).resolve() / "input.wav"
            source.write_bytes(b"RIFF-versioned-prepare")
            stdout = StringIO()
            stderr = StringIO()
            exit_code = cli_main(
                ["transcribe", "prepare", str(source), "--output", "json"],
                stdout=stdout,
                stderr=stderr,
                product_identity=fixture.product_identity,
                local_transcription_runtime=runtime,
            )
            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["result"]["status"], "ready")
            self.assertNotIn(
                "lulu_agent_cli_installation_ticket_v1",
                fixture.keychain.read_keys,
            )

    def test_production_factory_exposes_no_host_or_authority_injection(self) -> None:
        from src.core.cli_runtime.factory import (
            build_production_runtime,
            _build_verified_trusted_test_runtime,
        )

        self.assertEqual(
            tuple(inspect.signature(build_production_runtime).parameters),
            ("product_identity", "resource_root"),
        )
        self.assertNotIn(
            "link_host_broker",
            inspect.signature(_build_verified_trusted_test_runtime).parameters,
        )
        from src.core.cli_runtime.installation_ticket import (
            _system_application_support_root,
        )

        self.assertEqual(
            tuple(inspect.signature(_system_application_support_root).parameters),
            (),
        )
        app_root = Path(__file__).resolve().parents[1]
        for relative in (
            "src/core/cli_runtime/installation_ticket.py",
            "src/core/cli_runtime/factory.py",
        ):
            source = (app_root / relative).read_text(encoding="utf-8")
            for forbidden in ("Path.home", "os.environ", "expanduser"):
                self.assertNotIn(forbidden, source)

    def test_cli_drift_and_keychain_failure_leave_business_root_untouched(self) -> None:
        from src.core.cli_runtime.contracts import CliRuntimeTrustError
        from src.core.cli_runtime.factory import _build_legacy_trusted_test_runtime

        with TemporaryDirectory(prefix="lulu-cli-trusted-fail-") as directory:
            fixture = self._fixture(Path(directory).resolve())
            fixture.cli_path.write_bytes(b"tampered cli\n")
            with self.assertRaises(CliRuntimeTrustError) as tampered:
                _build_legacy_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    resource_root=fixture.resources,
                    cli_path=fixture.cli_path,
                    application_support_root=fixture.application_support,
                    keychain=fixture.keychain,
                    signature_verifier=FakeSignatureVerifier(fixture.signature),
                    now=lambda: fixture.now,
                )
            self.assertEqual(tampered.exception.code, "TRUST_ANCHOR_INVALID")
            self.assertEqual(list(fixture.application_support.iterdir()), [])
            self.assertEqual(fixture.keychain.mutations, 0)

            fixture.cli_path.write_bytes(b"canonical trusted-test cli\n")
            failing_keychain = FakeKeychainBoundary({}, fail_reads=True)
            with self.assertRaises(CliRuntimeTrustError) as unavailable:
                _build_legacy_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    resource_root=fixture.resources,
                    cli_path=fixture.cli_path,
                    application_support_root=fixture.application_support,
                    keychain=failing_keychain,
                    signature_verifier=FakeSignatureVerifier(fixture.signature),
                    now=lambda: fixture.now,
                )
            self.assertEqual(unavailable.exception.code, "SECURE_STORE_UNAVAILABLE")
            self.assertEqual(list(fixture.application_support.iterdir()), [])
            self.assertEqual(failing_keychain.mutations, 0)

            cache_locked = FakeKeychainBoundary(
                fixture.keychain.values,
                fail_after_reads=1,
            )
            with self.assertRaises(CliRuntimeTrustError) as cache_unavailable:
                _build_legacy_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    resource_root=fixture.resources,
                    cli_path=fixture.cli_path,
                    application_support_root=fixture.application_support,
                    keychain=cache_locked,
                    signature_verifier=FakeSignatureVerifier(fixture.signature),
                    now=lambda: fixture.now,
                )
            self.assertEqual(
                cache_unavailable.exception.code,
                "SECURE_STORE_UNAVAILABLE",
            )
            # No license-cache read remains. The failed second read now belongs
            # to job-integrity storage; empty private directories may exist, but
            # no business record or credential may be written.
            self.assertEqual(
                sorted(path.relative_to(fixture.application_support).as_posix()
                       for path in fixture.application_support.rglob("*")),
                ["Lulu", "Lulu/jobs"],
            )
            self.assertFalse(any(key.startswith("license_") for key in cache_locked.read_keys))
            self.assertEqual(cache_locked.mutations, 0)

    def test_external_developer_id_installation_remains_zero_capability(self) -> None:
        from src.core.cli_runtime.contracts import CliRuntimeTrustError
        from src.core.cli_runtime.factory import _build_legacy_trusted_test_runtime

        with TemporaryDirectory(prefix="lulu-cli-external-zero-") as directory:
            fixture = self._fixture(Path(directory).resolve())
            signature = replace(
                fixture.signature,
                signature_kind="developer_id",
                team_id="TEAM123456",
            )
            ticket = {
                "schema_id": "lulu.agent-cli-installation-ticket/v1",
                "schema_revision": 1,
                "trust_profile": "production",
                "bundle_id": "com.lulu.transcribe",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "canonical_cli_sha256": hashlib.sha256(
                    fixture.cli_path.read_bytes()
                ).hexdigest(),
                "installation_id": fixture.installation_id,
                "signature_kind": "developer_id",
                "certificate_sha256": signature.certificate_sha256,
                "cdhash": signature.cdhash,
                "designated_requirement": signature.designated_requirement,
                "team_id": "TEAM123456",
                "created_by": "verified_release_install",
                "user_authorized": True,
            }
            fixture.keychain.values[
                "lulu_agent_cli_installation_ticket_v1"
            ] = json.dumps(ticket, sort_keys=True, separators=(",", ":"))
            fixture.product_identity["shared"].update(
                {
                    "profile": "external_candidate",
                    "bundle_id": "com.lulu.transcribe",
                }
            )
            with (fixture.app_bundle / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.lulu.transcribe",
                        "CFBundleExecutable": "Lulu",
                        "CFBundlePackageType": "APPL",
                    },
                    handle,
                )

            with self.assertRaises(CliRuntimeTrustError) as blocked:
                _build_legacy_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    resource_root=fixture.resources,
                    cli_path=fixture.cli_path,
                    application_support_root=fixture.application_support,
                    keychain=fixture.keychain,
                    signature_verifier=FakeSignatureVerifier(signature),
                    now=lambda: fixture.now,
                )
            self.assertEqual(blocked.exception.code, "CAPABILITY_UNAVAILABLE")
            self.assertEqual(blocked.exception.reason_code, "TRUSTED_TEST_RUNTIME_ONLY")
            self.assertEqual(list(fixture.application_support.iterdir()), [])

    def test_frozen_product_entry_builds_runtime_without_caller_injection(self) -> None:
        from src.agent_cli import product_entry

        runtime = object()
        observed: dict[str, object] = {}

        def control_plane(_argv, **kwargs):
            observed.update(kwargs)
            return 0

        with TemporaryDirectory(prefix="lulu-cli-product-entry-") as directory:
            root = Path(directory).resolve()
            with (
                patch.object(product_entry, "project_product_identity", return_value={"adapter_id": "agent_cli", "shared": {"profile": "trusted_test"}}),
                patch.object(product_entry, "control_plane_main", side_effect=control_plane),
                patch("src.core.cli_runtime.factory.build_production_runtime", return_value=runtime) as build_runtime,
                patch.object(product_entry.sys, "frozen", True, create=True),
            ):
                self.assertEqual(product_entry.main([], resource_root=root), 0)

        build_runtime.assert_called_once()
        self.assertIs(observed["local_transcription_runtime"], runtime)

    def test_frozen_local_command_surfaces_closed_runtime_boot_error(self) -> None:
        from io import StringIO

        from src.agent_cli import product_entry
        from src.core.cli_runtime.contracts import CliRuntimeTrustError

        stdout = StringIO()
        stderr = StringIO()
        with TemporaryDirectory(prefix="lulu-cli-product-entry-error-") as directory:
            root = Path(directory).resolve()
            with (
                patch.object(
                    product_entry,
                    "project_product_identity",
                    return_value={
                        "adapter_id": "agent_cli",
                        "shared": {"profile": "trusted_test"},
                    },
                ),
                patch(
                    "src.core.cli_runtime.factory.build_production_runtime",
                    side_effect=CliRuntimeTrustError(
                        "SECURE_STORE_UNAVAILABLE",
                        "CLI_KEYCHAIN_READ_FAILED",
                    ),
                ),
                patch.object(product_entry.sys, "frozen", True, create=True),
            ):
                exit_code = product_entry.main(
                    ["transcribe", "prepare", "/tmp/input.wav", "--output", "json"],
                    resource_root=root,
                    stdout=stdout,
                    stderr=stderr,
                )

        self.assertEqual(exit_code, 10)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "SECURE_STORE_UNAVAILABLE")
        self.assertEqual(
            payload["error"]["details"]["reason_code"],
            "CLI_KEYCHAIN_READ_FAILED",
        )

    def test_local_transcribe_job_and_receipt_survive_runtime_reopen(self) -> None:
        from src.core.cli_runtime.factory import _build_legacy_trusted_test_runtime

        with TemporaryDirectory(prefix="lulu-cli-cross-process-") as directory:
            fixture = self._fixture(Path(directory).resolve())
            source = Path(directory).resolve() / "input.wav"
            source.write_bytes(b"RIFF-real-job-ledger-path")

            def pipeline(source_path: str, *, engine_name, progress, is_cancelled):
                self.assertEqual(source_path, str(source))
                self.assertEqual(engine_name, "local_sensevoice")
                self.assertFalse(is_cancelled())
                return Transcript(
                    text="跨进程转录。",
                    segments=[Segment("跨进程转录。", 0.0, 1.0)],
                    language="zh",
                    engine="local_sensevoice",
                    source=source_path,
                    title=None,
                    meta={},
                    saved_media=None,
                )

            ids = iter(("job-cross-001", "attempt-cross-001", "lineage-cross-001"))
            first = _build_legacy_trusted_test_runtime(
                product_identity=fixture.product_identity,
                resource_root=fixture.resources,
                cli_path=fixture.cli_path,
                application_support_root=fixture.application_support,
                keychain=fixture.keychain,
                signature_verifier=FakeSignatureVerifier(fixture.signature),
                now=lambda: fixture.now,
                pipeline_runner=pipeline,
                model_readiness_probe=lambda engine: engine == "local_sensevoice",
                id_factory=lambda _kind: next(ids),
            )
            completed = first.run(str(source))
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["job_receipt"]["job_id"], "job-cross-001")

            reopened = _build_legacy_trusted_test_runtime(
                product_identity=fixture.product_identity,
                resource_root=fixture.resources,
                cli_path=fixture.cli_path,
                application_support_root=fixture.application_support,
                keychain=fixture.keychain,
                signature_verifier=FakeSignatureVerifier(fixture.signature),
                now=lambda: fixture.now,
                pipeline_runner=pipeline,
                model_readiness_probe=lambda engine: engine == "local_sensevoice",
            )
            shown = reopened.jobs_show("job-cross-001")
            receipt = reopened.jobs_receipt("job-cross-001")
            self.assertEqual(shown["state"], "succeeded")
            self.assertEqual(shown["result_ref"], completed["result_ref"])
            self.assertEqual(receipt["receipt_ref"], shown["receipt_ref"])
            self.assertEqual(receipt["partial_failures"], [])

    def test_bundle_identifier_drift_is_rejected_before_keychain_or_jobs(self) -> None:
        from src.core.cli_runtime.contracts import CliRuntimeTrustError
        from src.core.cli_runtime.factory import _build_legacy_trusted_test_runtime

        with TemporaryDirectory(prefix="lulu-cli-bundle-drift-") as directory:
            fixture = self._fixture(Path(directory).resolve())
            with (fixture.app_bundle / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.attacker.substitute",
                        "CFBundleExecutable": "Lulu-Trusted-Test",
                        "CFBundlePackageType": "APPL",
                    },
                    handle,
                )
            with self.assertRaises(CliRuntimeTrustError) as blocked:
                _build_legacy_trusted_test_runtime(
                    product_identity=fixture.product_identity,
                    resource_root=fixture.resources,
                    cli_path=fixture.cli_path,
                    application_support_root=fixture.application_support,
                    keychain=fixture.keychain,
                    signature_verifier=FakeSignatureVerifier(fixture.signature),
                    now=lambda: fixture.now,
                )
            self.assertEqual(blocked.exception.reason_code, "BUNDLE_ID_MISMATCH")
            self.assertEqual(fixture.keychain.mutations, 0)
            self.assertEqual(list(fixture.application_support.iterdir()), [])

    def test_caller_owned_boolean_cannot_provision_installation_ticket(self) -> None:
        from src.core.cli_runtime.contracts import CliRuntimeTrustError
        from src.core.cli_runtime.keychain import (
            provision_local_trusted_test_credentials,
        )

        arguments = {
            "bundle_id": "com.lulu.transcribe.trusted-test",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "canonical_cli_sha256": "e" * 64,
            "signature": self._fixture_signature(),
            "hmac_key_factory": lambda: b"h" * 32,
            "epoch_factory": lambda: "f" * 32,
        }
        keychain = FakeKeychainBoundary({})
        with self.assertRaises(TypeError):
            provision_local_trusted_test_credentials(
                keychain=keychain,
                **arguments,
                user_authorized=True,
            )
        self.assertEqual(keychain.values, {})

        for forged in (True, {"issuer_source": "trusted_gui_human_action"}):
            with self.subTest(forged_type=type(forged).__name__):
                forged_keychain = FakeKeychainBoundary({})
                with self.assertRaises(CliRuntimeTrustError) as blocked:
                    provision_local_trusted_test_credentials(
                        keychain=forged_keychain,
                        **arguments,
                        approval_receipt=forged,
                    )
                self.assertEqual(blocked.exception.code, "APPROVAL_INVALID")
                self.assertEqual(forged_keychain.values, {})

    def test_authoritative_approval_is_bound_fresh_single_use_and_ticket_last(self) -> None:
        from src.core.cli_runtime.contracts import CliRuntimeTrustError
        from src.core.cli_runtime.keychain import (
            INSTALLATION_TICKET_KEY,
            provision_local_trusted_test_credentials,
        )

        approved_at = datetime.now(timezone.utc)
        keychain = FakeKeychainBoundary({})
        signature = self._fixture_signature()
        receipt = self._approval_receipt(
            signature=signature,
            installation_id="install-user-approved-001",
            nonce="approval-nonce-authoritative-0001",
            issued_at=approved_at,
            expires_at=approved_at + timedelta(seconds=60),
        )
        arguments = {
            "keychain": keychain,
            "bundle_id": "com.lulu.transcribe.trusted-test",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "canonical_cli_sha256": "e" * 64,
            "signature": signature,
            "approval_receipt": receipt,
            "hmac_key_factory": lambda: b"h" * 32,
            "epoch_factory": lambda: "f" * 32,
        }
        with self.assertRaises(CliRuntimeTrustError) as duplicate_nonce:
            self._approval_receipt(
                signature=signature,
                installation_id="install-duplicate-nonce-001",
                nonce="approval-nonce-authoritative-0001",
                issued_at=approved_at,
                expires_at=approved_at + timedelta(seconds=60),
            )
        self.assertEqual(duplicate_nonce.exception.code, "APPROVAL_CONSUMED")
        with self.assertRaises(TypeError):
            provision_local_trusted_test_credentials(
                **arguments,
                now=lambda: approved_at,
            )
        self.assertEqual(keychain.values, {})
        installation_id = provision_local_trusted_test_credentials(**arguments)
        self.assertEqual(installation_id, "install-user-approved-001")
        self.assertEqual(keychain.write_order[-1], INSTALLATION_TICKET_KEY)
        ticket = json.loads(keychain.values[INSTALLATION_TICKET_KEY])
        self.assertEqual(ticket["installation_id"], installation_id)
        self.assertEqual(ticket["canonical_cli_sha256"], "e" * 64)
        self.assertEqual(ticket["schema_revision"], 2)
        self.assertEqual(
            ticket["approval_issuer_source"], "trusted_gui_human_action"
        )
        self.assertEqual(ticket["approval_nonce"], "approval-nonce-authoritative-0001")
        self.assertNotIn("user_authorized", ticket)
        self.assertNotIn("h" * 32, keychain.values[INSTALLATION_TICKET_KEY])

        replay_keychain = FakeKeychainBoundary({})
        with self.assertRaises(CliRuntimeTrustError) as replayed:
            provision_local_trusted_test_credentials(
                **{**arguments, "keychain": replay_keychain}
            )
        self.assertEqual(replayed.exception.code, "APPROVAL_CONSUMED")
        self.assertEqual(replay_keychain.values, {})

        drift_receipt = self._approval_receipt(
            signature=signature,
            installation_id="install-binding-drift-001",
            nonce="approval-nonce-binding-drift-01",
            issued_at=approved_at,
            expires_at=approved_at + timedelta(seconds=60),
        )
        drift_keychain = FakeKeychainBoundary({})
        with self.assertRaises(CliRuntimeTrustError) as drifted:
            provision_local_trusted_test_credentials(
                **{
                    **arguments,
                    "keychain": drift_keychain,
                    "approval_receipt": drift_receipt,
                    "canonical_cli_sha256": "d" * 64,
                }
            )
        self.assertEqual(drifted.exception.code, "APPROVAL_INVALID")
        self.assertNotIn(INSTALLATION_TICKET_KEY, drift_keychain.values)

        expired_receipt = self._approval_receipt(
            signature=signature,
            installation_id="install-expired-approval-001",
            nonce="approval-nonce-expired-0000001",
            issued_at=approved_at - timedelta(seconds=120),
            expires_at=approved_at - timedelta(seconds=60),
        )
        expired_keychain = FakeKeychainBoundary({})
        with self.assertRaises(CliRuntimeTrustError) as expired:
            provision_local_trusted_test_credentials(
                **{
                    **arguments,
                    "keychain": expired_keychain,
                    "approval_receipt": expired_receipt,
                }
            )
        self.assertEqual(expired.exception.code, "APPROVAL_EXPIRED")
        self.assertNotIn(INSTALLATION_TICKET_KEY, expired_keychain.values)

        for write_number in (1, 2, 3):
            with self.subTest(keychain_write_failure=write_number):
                failing_receipt = self._approval_receipt(
                    signature=signature,
                    installation_id=f"install-keychain-failure-{write_number:03d}",
                    nonce=f"approval-nonce-keychain-failure-{write_number:02d}",
                    issued_at=approved_at,
                    expires_at=approved_at + timedelta(seconds=60),
                )
                failing_keychain = FakeKeychainBoundary(
                    {}, fail_write_at=write_number
                )
                with self.assertRaises(CliRuntimeTrustError) as unavailable:
                    provision_local_trusted_test_credentials(
                        **{
                            **arguments,
                            "keychain": failing_keychain,
                            "approval_receipt": failing_receipt,
                        }
                    )
                self.assertEqual(
                    unavailable.exception.code, "SECURE_STORE_UNAVAILABLE"
                )
                self.assertNotIn(INSTALLATION_TICKET_KEY, failing_keychain.values)

    @staticmethod
    def _approval_receipt(
        *,
        signature,
        installation_id: str,
        nonce: str,
        issued_at: datetime,
        expires_at: datetime,
    ):
        from src.core.cli_runtime.approval import (
            _issue_from_trusted_gui_verified_human_action,
        )

        return _issue_from_trusted_gui_verified_human_action(
            bundle_id="com.lulu.transcribe.trusted-test",
            source_commit="a" * 40,
            source_tree="b" * 40,
            canonical_cli_sha256="e" * 64,
            installation_id=installation_id,
            signature=signature,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    @staticmethod
    def _fixture_signature():
        from src.core.cli_runtime.contracts import CodeSignatureEvidenceV1

        return CodeSignatureEvidenceV1(
            signature_kind="ad_hoc",
            certificate_sha256="",
            cdhash="c" * 40,
            designated_requirement="identifier com.lulu.transcribe.trusted-test",
            team_id="",
        )


if __name__ == "__main__":
    unittest.main()
