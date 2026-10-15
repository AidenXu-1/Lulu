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
