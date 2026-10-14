from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import inspect
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.core.cli_runtime.contracts import CodeSignatureEvidenceV1
from tests.test_cli_versioned_installation_ticket import (
    FakeVersionedKeychain,
    _authority,
    _final_acl_receipt,
    _patched_test_authority_validation,
    _validate_test_authority,
)


class _SignatureVerifier:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def verify(self, *, app_bundle, cli_path):
        self.calls.append((Path(app_bundle), Path(cli_path)))
        return self.evidence


class _FakeAcl:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def verify_owned_item(self, *, item_receipt, cdhash):
        if self.fail_at == len(self.calls) + 1:
            raise RuntimeError("bind failed")
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        claims = _owned_item_claims(item_receipt)
        account = claims.authority.accounts[claims.account_index]
        self.calls.append(("Lulu", account, cdhash))


def _created_receipt(locator, plan, index=0):
    from src.core.cli_runtime.installation_ticket import _new_ownership_intents

    authority = _validate_test_authority(locator_receipt=locator, plan_receipt=plan)
    keychain = FakeVersionedKeychain()
    final_acl = _final_acl_receipt(locator, plan)
    intents = _new_ownership_intents(authority, final_acl)
    return keychain.create_owned_item(
        final_acl,
        intents[index],
        "opaque-secret",
    )


def _fixture(root: Path):
    app = root / "Lulu Trusted Test.app"
    macos = app / "Contents/MacOS"
    macos.mkdir(parents=True)
    cli = macos / "lulu-agent-cli"
    cli.write_bytes(b"versioned cli binding fixture\n")
    cli.chmod(0o700)
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.lulu.transcribe.trusted-test"}, handle)
    signature = CodeSignatureEvidenceV1(
        signature_kind="ad_hoc",
        certificate_sha256="",
        cdhash="a" * 40,
        designated_requirement='cdhash H"' + "a" * 40 + '"',
        team_id="",
    )
    candidate = {
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "product_version": "0.3.2",
        "build": "20260823.1",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "app_content_tree_sha256": "3" * 64,
        "app_profile_tree_sha256": "4" * 64,
        "canonical_cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        "signature": {
            "signature_kind": signature.signature_kind,
            "certificate_sha256": signature.certificate_sha256,
            "cdhash": signature.cdhash,
            "designated_requirement": signature.designated_requirement,
            "team_id": signature.team_id,
        },
    }
    support = root / "Application Support"
    support.mkdir(mode=0o700)
    locator, plan = _authority(
        support,
        "acl-fixture-nonce",
        candidate=candidate,
        installation_target=app,
    )
    return app, cli, signature, locator, plan


class KeychainAclBindingTests(unittest.TestCase):
    def test_acl_derives_app_and_cli_only_from_authority(self):
        from src.core.cli_runtime.macos_keychain_acl import bind_versioned_keychain_acl

        parameters = inspect.signature(bind_versioned_keychain_acl).parameters
        self.assertNotIn("app_bundle", parameters)
        self.assertNotIn("cli_path", parameters)
        self.assertTrue({"locator_receipt", "plan_receipt"}.issubset(parameters))

    def test_final_acl_intent_is_exact_two_entry_private_canonical(self):
        import pickle
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )
        from src.core.cli_runtime.macos_keychain_acl import (
            _issue_memory_final_acl_intent,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-canonical-") as directory:
            _app, _cli, _signature, locator, plan = _fixture(
                Path(directory).resolve()
            )
            receipt = _issue_memory_final_acl_intent(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            claims = _final_acl_intent_claims(receipt)
            payload = json.loads(claims.canonical_bytes)
            self.assertEqual(payload["entry_count"], 2)
            self.assertEqual(
                [entry["entry_role"] for entry in payload["entries"]],
                ["partition_id", "trusted_application"],
            )
            self.assertEqual(
                payload["owner"],
                {
                    "gid": os.getegid(),
                    "owner_type": ["kSecUseOnlyUID"],
                    "uid": os.geteuid(),
                },
            )
            self.assertEqual(payload["entries"][0]["prompt_selector"], 0)
            self.assertEqual(payload["entries"][1]["prompt_selector"], 0)
            self.assertEqual(
                payload["entries"][0]["authorizations"],
                ["kSecACLAuthorizationPartitionID"],
            )
            self.assertEqual(
                payload["entries"][1]["authorizations"],
                ["kSecACLAuthorizationAny"],
            )
            with self.assertRaises(TypeError):
                pickle.dumps(receipt)

    def test_acl_binds_each_exact_owned_receipt_to_live_cli_cdhash(self):
        from src.core.cli_runtime.macos_keychain_acl import bind_versioned_keychain_acl

        with TemporaryDirectory(prefix="lulu-acl-bind-") as directory:
            app, cli, signature, locator, plan = _fixture(Path(directory).resolve())
            authority = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            acl = _FakeAcl()
            with _patched_test_authority_validation():
                results = [
                    bind_versioned_keychain_acl(
                        locator_receipt=locator,
                        plan_receipt=plan,
                        signature_verifier=_SignatureVerifier(signature),
                        acl_boundary=acl,
                        item_receipt=_created_receipt(locator, plan, index),
                    )
                    for index in range(3)
                ]

            result = results[-1]
            self.assertTrue(all(item["state"] == "bound" for item in results))
            self.assertEqual(
                result["target_binding_sha256"],
                authority.target_binding_sha256,
            )
            self.assertEqual(result["installation_target"], str(app))
            self.assertEqual(result["target_role"], "trusted_test")
            self.assertEqual(
                acl.calls,
                [("Lulu", account, signature.cdhash) for account in authority.accounts],
            )
            self.assertNotIn("password", json.dumps(result).lower())

    def test_sha_or_cdhash_drift_causes_zero_acl_mutation(self):
        from src.core.cli_runtime.macos_keychain_acl import (
            KeychainAclBindingError,
            bind_versioned_keychain_acl,
        )

        with TemporaryDirectory(prefix="lulu-acl-drift-") as directory:
            app, cli, signature, locator, plan = _fixture(Path(directory).resolve())
            cli.write_bytes(b"tampered")
            acl = _FakeAcl()
            with _patched_test_authority_validation():
                with self.assertRaises(KeychainAclBindingError) as sha_drift:
                    bind_versioned_keychain_acl(
                        locator_receipt=locator,
                        plan_receipt=plan,
                        signature_verifier=_SignatureVerifier(signature),
                        acl_boundary=acl,
                        item_receipt=_created_receipt(locator, plan),
                    )
            self.assertEqual(sha_drift.exception.reason_code, "CANDIDATE_IDENTITY_MISMATCH")
            self.assertEqual(acl.calls, [])

            app, cli, signature, locator, plan = _fixture(
                Path(directory).resolve() / "second"
            )
            acl = _FakeAcl()
            drifted = replace(signature, cdhash="b" * 40)
            with _patched_test_authority_validation():
                with self.assertRaises(KeychainAclBindingError):
                    bind_versioned_keychain_acl(
                        locator_receipt=locator,
                        plan_receipt=plan,
                        signature_verifier=_SignatureVerifier(drifted),
                        acl_boundary=acl,
                        item_receipt=_created_receipt(locator, plan),
                    )
            self.assertEqual(acl.calls, [])

    def test_native_constructor_is_platform_gated_before_effect(self):
        from src.core.cli_runtime.macos_keychain_acl import (
            KeychainAclBindingError,
            _issue_native_final_acl_intent,
        )

        boundary = Mock()
        with TemporaryDirectory(prefix="lulu-final-acl-platform-") as directory:
            _app, _cli, _signature, locator, plan = _fixture(
                Path(directory).resolve()
            )
            with patch(
                "src.core.cli_runtime.macos_keychain_acl.sys.platform", "linux"
            ):
                with self.assertRaises(KeychainAclBindingError) as blocked:
                    _issue_native_final_acl_intent(
                        locator_receipt=locator,
                        plan_receipt=plan,
                        native_boundary=boundary,
                    )
            self.assertEqual(
                blocked.exception.reason_code,
                "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE",
            )
            boundary.construct_final_acl.assert_not_called()

    def test_native_constructor_receipt_carries_same_self_checked_access(self):
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )
        from src.core.cli_runtime.macos_keychain_acl import (
            _canonical_test_final_acl,
            _issue_native_final_acl_intent,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-native-") as directory:
            _app, _cli, _signature, locator, plan = _fixture(
                Path(directory).resolve()
            )
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            access_ref = object()
            boundary = Mock()
            boundary.construct_final_acl.return_value = (
                _canonical_test_final_acl(authority),
                access_ref,
            )
            with (
                patch("src.core.cli_runtime.macos_keychain_acl.sys.platform", "darwin"),
                patch(
                    "src.core.cli_runtime.macos_keychain_acl.platform.machine",
                    return_value="arm64",
                ),
                patch(
                    "src.core.cli_runtime.macos_keychain_acl.platform.mac_ver",
                    return_value=("15.7.0", ("", "", ""), ""),
                ),
                patch(
                    "src.core.cli_runtime.macos_keychain_acl._validate_live_candidate"
                ) as validate_live,
            ):
                receipt = _issue_native_final_acl_intent(
                    locator_receipt=locator,
                    plan_receipt=plan,
                    native_boundary=boundary,
                )
            claims = _final_acl_intent_claims(receipt, authority=authority)
            self.assertIs(claims.access_ref, access_ref)
            self.assertEqual(
                hashlib.sha256(claims.canonical_bytes).hexdigest(),
                claims.expected_final_acl_sha256,
            )
            boundary.construct_final_acl.assert_called_once_with(authority)
            validate_live.assert_called_once_with(
                Path(authority.installation_target), authority.candidate
            )

    def test_source_has_explicit_constructor_and_no_legacy_mutation_route(self):
        from src.core.cli_runtime import macos_keychain_acl as module
        from src.core.license import store

        source = Path(module.__file__).read_text(encoding="utf-8")
        store_source = Path(store.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "/usr/bin/security",
            "set-generic-password-partition-list",
            "MacOSPartitionListAclBoundary",
            "SecKeychainItemSetAccess",
        ):
            self.assertNotIn(forbidden, source + store_source)
        for required in (
            "SecAccessCreateWithOwnerAndACL",
            "SecACLCreateWithSimpleContents",
            "SecACLUpdateAuthorizations",
            "SecAccessCopyOwnerAndACL",
            "SecACLCopyAuthorizations",
            "SecTrustedApplicationSetData",
        ):
            self.assertIn(required, store_source)


if __name__ == "__main__":
    unittest.main()
