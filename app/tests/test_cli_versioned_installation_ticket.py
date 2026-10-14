from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


_TEST_AUTHORITY_SEAL = object()


@dataclass(frozen=True, slots=True)
class _TestLocatorReceipt:
    seal: object
    authority: object


@dataclass(frozen=True, slots=True)
class _TestPlanReceipt:
    seal: object
    authority: object
    plan_bytes: bytes


class FakeVersionedKeychain:
    def __init__(
        self,
        values: dict[str, str] | None = None,
        *,
        fail_write_at: int | None = None,
        state: dict[str, object] | None = None,
    ) -> None:
        from src.core.license.store import MemorySecureStore

        self._memory = MemorySecureStore(state=state)
        self.values = self._memory.values
        self.values.update(values or {})
        self.fail_write_at = fail_write_at
        self.calls: list[tuple[str, str]] = []
        self.write_order: list[str] = []

    def get_text(self, key: str) -> str | None:
        self.calls.append(("get", key))
        return self.values.get(key)

    def write_new(self, key: str, value: str) -> None:
        self.calls.append(("write_new", key))
        if self.fail_write_at == len(self.write_order) + 1:
            raise RuntimeError("injected write failure")
        if key in self.values:
            raise RuntimeError("account exists")
        self.values[key] = value
        self.write_order.append(key)

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        self.calls.append(("delete", key))
        if key not in self.values and not missing_ok:
            raise RuntimeError("account missing")
        self.values.pop(key, None)

    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object:
        self.calls.append(("prepare_final_acl", "authority"))
        return self._memory.prepare_final_acl_intent(
            locator_receipt,
            plan_receipt,
        )

    def create_owned_item(
        self,
        final_acl_intent_receipt: object,
        intent_receipt: object,
        secret_value: str,
    ) -> object:
        from src.core.cli_runtime.installation_ticket import _ownership_intent_claims

        claims = _ownership_intent_claims(intent_receipt)
        account = claims.authority.accounts[claims.account_index]
        self.calls.append(("create_owned", account))
        if self.fail_write_at == len(self.write_order) + 1:
            raise RuntimeError("injected write failure")
        receipt = self._memory.create_owned_item(
            final_acl_intent_receipt,
            intent_receipt,
            secret_value,
        )
        self.write_order.append(account)
        return receipt

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object:
        from src.core.cli_runtime.installation_ticket import _ownership_intent_claims

        claims = _ownership_intent_claims(intent_receipt)
        account = claims.authority.accounts[claims.account_index]
        self.calls.append(("inspect_exact", account))
        return self._memory.inspect_exact_item(
            final_acl_intent_receipt,
            intent_receipt,
        )

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None:
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        claims = _owned_item_claims(receipt)
        account = claims.authority.accounts[claims.account_index]
        self.calls.append(("delete_verified", account))
        self._memory.delete_verified_item(final_acl_intent_receipt, receipt)


def _authority(
    root: Path,
    nonce: str = "fixture-nonce-authority",
    *,
    candidate: dict[str, object] | None = None,
    installation_target: Path | str = "/Applications/Lulu Trusted Test.app",
):
    from src.core.cli_runtime.installation_ticket import (
        JOB_ANCHOR_KEY_PREFIX,
        JOB_HMAC_KEY_PREFIX,
        PLAN_FILE,
        PLAN_RELATIVE_ROOT,
        VERSIONED_TICKET_PREFIX,
        ValidatedAuthority,
        canonical_json_bytes,
        _protected_formal_app_snapshot,
    )

    selected = json.loads(json.dumps(candidate or {
        "architecture": "arm64",
        "app_content_tree_sha256": "1" * 64,
        "app_profile_tree_sha256": "2" * 64,
        "build": "20260823.1",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "canonical_cli_sha256": "3" * 64,
        "channel": "trusted-test",
        "product_name": "Lulu Trusted Test",
        "product_version": "0.3.2",
        "profile": "trusted_test",
        "signature": {
            "signature_kind": "ad_hoc",
            "certificate_sha256": "",
            "cdhash": "4" * 40,
            "designated_requirement": f'cdhash H"{"4" * 40}"',
            "team_id": "",
        },
    }))
    selected_source_commit = str(selected.pop("source_commit", "5" * 40))
    selected_source_tree = str(selected.pop("source_tree", "6" * 40))
    selected.setdefault("architecture", "arm64")
    selected.setdefault("channel", "trusted-test")
    selected.setdefault("product_name", "Lulu Trusted Test")
    selected.setdefault("profile", "trusted_test")
    carrier_sha = "a" * 64
    authority_sha = "b" * 64
    review_receipts = {
        "security_app_carrier_sha256": "c" * 64,
        "security_authority_artifact_sha256": authority_sha,
        "test_app_carrier_sha256": "d" * 64,
        "test_authority_artifact_sha256": authority_sha,
    }
    source_identity = {
        "actual_build_source_commit": selected_source_commit,
        "actual_build_source_tree": selected_source_tree,
        "product_source_sha256": "7" * 64,
        "reviewed_base_commit": "17503b8ee209cbc7989f2853243899cd2082755b",
        "reviewed_base_tree": "8ca2f2021ebdb1159d13dd825710e655bc95bdad",
    }
    authority_identity = {
        "artifact_sha256": authority_sha,
        "source_commit": "8" * 40,
        "source_tree": "9" * 40,
    }
    selected.update(
        {
            "authority_artifact_sha256": authority_sha,
            "authority_identity": authority_identity,
            "carrier_sha256": carrier_sha,
            "review_receipts": review_receipts,
            "source_identity": source_identity,
        }
    )
    target_identity = {
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "channel": "trusted-test",
        "product_name": "Lulu Trusted Test",
        "profile": "trusted_test",
    }
    selected["target_role"] = "trusted_test"
    selected["installation_target"] = str(installation_target)
    candidate_bytes = canonical_json_bytes(selected)
    binding = hashlib.sha256(candidate_bytes).hexdigest()
    installation_id = "lulu-tt-" + hashlib.sha256(
        candidate_bytes + nonce.encode("utf-8")
    ).hexdigest()[:32]
    accounts = (
        JOB_HMAC_KEY_PREFIX + installation_id,
        JOB_ANCHOR_KEY_PREFIX + installation_id,
        VERSIONED_TICKET_PREFIX + installation_id,
    )
    target_binding = hashlib.sha256(
        canonical_json_bytes(
            {
                "candidate_binding_sha256": binding,
                "installation_id": installation_id,
                "installation_target": str(installation_target),
                "target_role": "trusted_test",
            }
        )
    ).hexdigest()
    target_path = Path(installation_target)
    formal_app_snapshot = (
        {"kind": "absent"}
        if target_path.parent == Path("/Applications")
        else _protected_formal_app_snapshot(target_path.parent / "Lulu.app")
    )
    directory = root / PLAN_RELATIVE_ROOT / binding
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (root, root / "Lulu", root / "Lulu/installation-trust", root / PLAN_RELATIVE_ROOT, directory):
        os.chmod(parent, 0o700)
    plan_path = directory / PLAN_FILE
    locator_payload = {
        "accounts": list(accounts),
        "candidate_binding_sha256": binding,
        "installation_id": installation_id,
        "plan_path": str(plan_path),
        "schema": "lulu.agent-cli-authoritative-locator-public/v1",
        "target_identity": target_identity,
        "target_role": "trusted_test",
        "installation_target": str(installation_target),
        "target_binding_sha256": target_binding,
        "formal_app_snapshot": formal_app_snapshot,
    }
    locator_sha = hashlib.sha256(canonical_json_bytes(locator_payload)).hexdigest()
    payload = {
        "accounts": list(accounts),
        "authority_artifact_sha256": authority_sha,
        "candidate": selected,
        "carrier_sha256": carrier_sha,
        "contains_secret_material": False,
        "installation_id": installation_id,
        "installation_target": str(installation_target),
        "legacy_fixed_ticket_action": "preserve_presence_only",
        "locator_sha256": locator_sha,
        "review_receipts": review_receipts,
        "schema": "lulu.r16-post-build-authority-ticket-plan/v1",
        "source_identity": source_identity,
        "authority_identity": authority_identity,
        "state": "planned_before_keychain_write",
        "target_binding_sha256": target_binding,
        "target_identity": target_identity,
        "target_role": "trusted_test",
        "formal_app_snapshot": formal_app_snapshot,
        "ticket_written_last": True,
    }
    raw = canonical_json_bytes(payload)
    try:
        descriptor = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        from src.core.cli_runtime.installation_ticket import AuthorityError

        raise AuthorityError("INSTALLATION_PLAN_CONFLICT") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
    authority = ValidatedAuthority(
        candidate_binding_sha256=binding,
        installation_id=installation_id,
        accounts=accounts,
        plan_path=str(plan_path),
        locator_sha256=locator_sha,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        plan_size=len(raw),
        candidate=selected,
        carrier_sha256=carrier_sha,
        authority_artifact_sha256=authority_sha,
        review_receipts=review_receipts,
        source_identity=source_identity,
        authority_identity=authority_identity,
        target_identity=target_identity,
        target_role="trusted_test",
        installation_target=str(installation_target),
        target_binding_sha256=target_binding,
        formal_app_snapshot=formal_app_snapshot,
    )
    return (
        _TestLocatorReceipt(_TEST_AUTHORITY_SEAL, authority),
        _TestPlanReceipt(_TEST_AUTHORITY_SEAL, authority, raw),
    )


def _validate_test_authority(
    *,
    locator_receipt: object,
    plan_receipt: object,
    post_build_receipt: object | None = None,
):
    from src.core.cli_runtime.installation_ticket import AuthorityError

    if (
        type(locator_receipt) is not _TestLocatorReceipt
        or type(plan_receipt) is not _TestPlanReceipt
        or locator_receipt.seal is not _TEST_AUTHORITY_SEAL
        or plan_receipt.seal is not _TEST_AUTHORITY_SEAL
        or locator_receipt.authority is not plan_receipt.authority
        or (post_build_receipt is not None and post_build_receipt is not locator_receipt)
    ):
        raise TypeError("test-private authoritative receipts required")
    authority = locator_receipt.authority
    path = Path(authority.plan_path)
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or raw != plan_receipt.plan_bytes
        or len(raw) != authority.plan_size
        or hashlib.sha256(raw).hexdigest() != authority.plan_sha256
    ):
        raise AuthorityError("INSTALLATION_PLAN_INTEGRITY_ERROR")
    return authority


def _reload_test_authority(locator: object, plan: object):
    authority = _validate_test_authority(
        locator_receipt=locator,
        plan_receipt=plan,
    )
    return (
        _TestLocatorReceipt(_TEST_AUTHORITY_SEAL, authority),
        _TestPlanReceipt(_TEST_AUTHORITY_SEAL, authority, plan.plan_bytes),
    )


@contextmanager
def _patched_test_authority_validation():
    import src.core.cli_runtime.factory as factory
    import src.core.cli_runtime.installation_recovery as recovery
    import src.core.cli_runtime.installation_ticket as ticket
    import src.core.cli_runtime.keychain as keychain
    import src.core.cli_runtime.macos_keychain_acl as acl

    with ExitStack() as stack:
        for module in (ticket, keychain, acl, recovery, factory):
            stack.enter_context(
                mock.patch.object(module, "validate_authority", _validate_test_authority)
            )
        yield


# The inherited recovery tests import this module's private `_authority` fixture
# but are intentionally outside the R2 write set.  Install the strict Fake
# validator at test-module load time so those consumers never need production
# registry entries or a product compatibility shim.
import src.core.cli_runtime.installation_ticket as _test_ticket_module
import src.core.cli_runtime.installation_recovery as _test_recovery_module
import src.core.cli_runtime.keychain as _test_keychain_module
import src.core.cli_runtime.macos_keychain_acl as _test_acl_module

_test_ticket_module.validate_authority = _validate_test_authority
_test_recovery_module.validate_authority = _validate_test_authority
_test_keychain_module.validate_authority = _validate_test_authority
_test_acl_module.validate_authority = _validate_test_authority


def _final_acl_receipt(
    locator_receipt: object,
    plan_receipt: object,
    *,
    access_ref: object | None = None,
) -> object:
    if access_ref is None:
        return _test_acl_module._issue_memory_final_acl_intent(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    authority = _validate_test_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    return _test_ticket_module._register_final_acl_intent(
        authority,
        canonical_bytes=_test_acl_module._canonical_test_final_acl(authority),
        access_ref=access_ref,
    )


class VersionedInstallationTicketAuthorityTests(unittest.TestCase):
    def test_final_acl_authority_is_frozen_before_all_three_ownership_envelopes(self) -> None:
        from src.core.cli_runtime.keychain import (
            provision_versioned_installation_credentials,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-envelope-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "final-acl-envelope")
            keychain = FakeVersionedKeychain()
            with _patched_test_authority_validation():
                provision_versioned_installation_credentials(
                    post_build_receipt=locator,
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                )

            envelopes = [
                json.loads(item["envelope"])
                for item in keychain._memory._owned.values()
            ]
            self.assertEqual(len(envelopes), 3)
            expected = {item["expected_final_acl_sha256"] for item in envelopes}
            self.assertEqual(len(expected), 1)
            self.assertRegex(expected.pop(), r"^[0-9a-f]{64}$")
            self.assertTrue(
                all("expected_acl_policy_sha256" not in item for item in envelopes)
            )
            self.assertTrue(
                all("raw_data_b64" not in json.dumps(item) for item in envelopes)
            )

    def test_final_acl_digest_or_duck_receipt_has_zero_keychain_effect(self) -> None:
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-duck-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "final-acl-duck")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            keychain = FakeVersionedKeychain()
            for forged in (
                "a" * 64,
                {"expected_final_acl_sha256": "a" * 64},
                object(),
            ):
                with self.subTest(forged=type(forged).__name__):
                    with self.assertRaises(TypeError):
                        _new_ownership_intents(authority, forged)
            self.assertEqual(keychain._memory.create_count, 0)
            self.assertEqual(keychain._memory.inventory_count, 0)

    def test_final_acl_receipt_is_full_authority_bound(self) -> None:
        from src.core.cli_runtime.installation_ticket import (
            AuthorityError,
            _new_ownership_intents,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-authority-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator_a, plan_a = _authority(root / "a", "final-acl-a")
            locator_b, plan_b = _authority(root / "b", "final-acl-b")
            receipt = _final_acl_receipt(locator_a, plan_a)
            authority_b = _validate_test_authority(
                locator_receipt=locator_b,
                plan_receipt=plan_b,
            )
            with self.assertRaises(AuthorityError) as blocked:
                _new_ownership_intents(authority_b, receipt)
            self.assertEqual(
                blocked.exception.reason_code,
                "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR",
            )

    def test_final_acl_canonical_metadata_drift_cannot_be_registered(self) -> None:
        from src.core.cli_runtime.installation_ticket import (
            AuthorityError,
            _final_acl_intent_claims,
            _register_final_acl_intent,
            canonical_json_bytes,
        )

        with TemporaryDirectory(prefix="lulu-final-acl-register-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "final-acl-register")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            valid = _final_acl_receipt(locator, plan)
            payload = json.loads(_final_acl_intent_claims(valid).canonical_bytes)
            payload["entries"][1]["prompt_selector"] = 1
            with self.assertRaises(AuthorityError) as blocked:
                _register_final_acl_intent(
                    authority,
                    canonical_bytes=canonical_json_bytes(payload),
                    access_ref=object(),
                )
            self.assertEqual(
                blocked.exception.reason_code,
                "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR",
            )

    def test_v3_ticket_write_and_exact_readback_bind_physical_target(self) -> None:
        from src.core.cli_runtime.keychain import (
            load_versioned_installation_ticket,
            provision_versioned_installation_credentials,
        )

        with TemporaryDirectory(prefix="lulu-target-bound-ticket-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "target-bound-ticket-nonce")
            claims = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            keychain = FakeVersionedKeychain()
            with _patched_test_authority_validation():
                provision = provision_versioned_installation_credentials(
                    post_build_receipt=locator,
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                    hmac_key_factory=lambda: b"t" * 32,
                    epoch_factory=lambda: "e" * 32,
                )
                ticket = load_versioned_installation_ticket(
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                )
            self.assertEqual(ticket["target_role"], claims.target_role)
            self.assertEqual(ticket["installation_target"], claims.installation_target)
            self.assertEqual(
                ticket["target_binding_sha256"], claims.target_binding_sha256
            )
            self.assertEqual(ticket["profile"], "trusted_test")
            self.assertEqual(ticket["channel"], "trusted-test")
            self.assertEqual(ticket["product_name"], "Lulu Trusted Test")
            self.assertEqual(
                provision["target_binding_sha256"],
                claims.target_binding_sha256,
            )
            self.assertEqual(
                provision["installation_target"], claims.installation_target
            )

    def test_owned_api_has_no_account_delete_or_upsert_and_uses_atomic_final_acl_ticket_last(self):
        import inspect
        from src.core.cli_runtime import keychain as module
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        protocol_source = inspect.getsource(module.VersionedKeychainBoundary)
        self.assertIn("create_owned_item", protocol_source)
        self.assertIn("inspect_exact_item", protocol_source)
        self.assertIn("delete_verified_item", protocol_source)
        self.assertNotIn("write_new", protocol_source)
        self.assertNotIn("delete_text", protocol_source)

        with TemporaryDirectory(prefix="lulu-owned-ticket-order-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "owned-ticket-order")
            keychain = FakeVersionedKeychain()
            legacy_bind_calls = []

            def bind(receipt):
                legacy_bind_calls.append(_owned_item_claims(receipt).account_index)

            with _patched_test_authority_validation():
                result = module.provision_versioned_installation_credentials(
                    post_build_receipt=locator,
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                    bind_created_item=bind,
                )
            self.assertEqual(legacy_bind_calls, [])
            self.assertTrue(result["ticket_written_last"])
            self.assertEqual(keychain._memory.secret_read_count, 0)
            self.assertEqual(keychain._memory.create_count, 3)
            self.assertEqual(
                [call for call in keychain.calls if call[0] == "prepare_final_acl"],
                [("prepare_final_acl", "authority")],
            )

    def test_public_runtime_package_exposes_no_authority_issuer_or_registry_writer(self) -> None:
        import src.core.cli_runtime as public_runtime

        forbidden = {
            "FrozenCandidateVerifier",
            "persist_plan_before_keychain_write",
            "validate_authority",
        }
        self.assertTrue(forbidden.isdisjoint(set(public_runtime.__all__)))
        for name in forbidden:
            self.assertFalse(hasattr(public_runtime, name), name)

    def test_post_build_authority_is_exact_type_registry_and_bytes_bound(self) -> None:
        import inspect
        import src.core.cli_runtime.installation_ticket as authority

        self.assertFalse(hasattr(authority, "_register_measured_evidence"))
        issuance_source = inspect.getsource(
            authority._register_post_build_authority_reviews
        )
        self.assertIn("_SecurityAuthorityArtifactReviewReceipt", issuance_source)
        self.assertIn("_TestAuthorityArtifactReviewReceipt", issuance_source)
        self.assertIn("_CORE_POST_BUILD_REGISTRY[token] = claims", issuance_source)
        self.assertIn("_PostBuildAuthorityReceipt", issuance_source)
        with self.assertRaises(TypeError):
            authority._register_post_build_authority_reviews({}, {})
        with self.assertRaises(TypeError):
            authority._post_build_claims({"candidate_bytes": "forged"})

    def test_verified_candidate_persists_create_new_plan_and_reloads_authority(self) -> None:
        from src.core.cli_runtime.installation_ticket import (
            plan_public_fingerprint,
        )

        with TemporaryDirectory(prefix="lulu-versioned-ticket-") as directory:
            support = Path(directory).resolve()
            os.chmod(support, 0o700)
            locator, plan = _authority(support, "fixture-nonce-0001")
            claims = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            with _patched_test_authority_validation():
                public = plan_public_fingerprint(
                    locator_receipt=locator,
                    plan_receipt=plan,
                )

            plan_path = Path(claims.plan_path)
            self.assertEqual(
                plan_path,
                support
                / "Lulu/installation-trust/agent-cli"
                / claims.candidate_binding_sha256
                / "ticket-plan.json",
            )
            self.assertEqual(stat.S_IMODE(plan_path.lstat().st_mode), 0o600)
            self.assertFalse(plan_path.is_symlink())
            self.assertEqual(public["plan_sha256"], claims.plan_sha256)
            self.assertEqual(len(claims.accounts), 3)
            self.assertTrue(claims.accounts[2].startswith(
                "lulu_agent_cli_installation_ticket_v3:"
            ))

            reloaded_locator, reloaded_plan = _reload_test_authority(locator, plan)
            reloaded = _validate_test_authority(
                locator_receipt=reloaded_locator,
                plan_receipt=reloaded_plan,
            )
            self.assertEqual(reloaded.plan_sha256, claims.plan_sha256)
            self.assertEqual(reloaded.accounts, claims.accounts)

    def test_existing_plan_is_preserved_and_plain_data_has_no_authority(self) -> None:
        from src.core.cli_runtime.installation_ticket import (
            AuthorityError,
        )

        with TemporaryDirectory(prefix="lulu-versioned-conflict-") as directory:
            support = Path(directory).resolve()
            os.chmod(support, 0o700)
            locator, plan = _authority(support, "fixture-nonce-0002")
            claims = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            path = Path(claims.plan_path)
            before = (path.read_bytes(), path.lstat().st_ino)

            with self.assertRaises(AuthorityError) as conflict:
                _authority(support, "fixture-nonce-0003")
            self.assertEqual(conflict.exception.reason_code, "INSTALLATION_PLAN_CONFLICT")
            self.assertEqual((path.read_bytes(), path.lstat().st_ino), before)

            with self.assertRaises((TypeError, AuthorityError)):
                _validate_test_authority(
                    locator_receipt={"path": str(path)},
                    plan_receipt={"sha256": claims.plan_sha256},
                )

    def test_versioned_provision_is_ticket_last_and_preserves_legacy_unknown(self) -> None:
        from src.core.cli_runtime.keychain import (
            load_versioned_installation_ticket,
            provision_versioned_installation_credentials,
        )

        legacy = "legacy-ticket-bytes-must-stay"
        unknown = "unknown-account-bytes-must-stay"
        with TemporaryDirectory(prefix="lulu-versioned-provision-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root)
            claims = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            keychain = FakeVersionedKeychain(
                {
                    "lulu_agent_cli_installation_ticket_v1": legacy,
                    "unrelated-user-account": unknown,
                }
            )

            with _patched_test_authority_validation():
                result = provision_versioned_installation_credentials(
                    post_build_receipt=locator,
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                )
                ticket = load_versioned_installation_ticket(
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                )

            self.assertEqual(result["state"], "provisioned_versioned")
            self.assertEqual(tuple(keychain.write_order), claims.accounts)
            self.assertEqual(keychain.write_order[-1], claims.accounts[2])
            self.assertEqual(ticket["schema_id"], "lulu.agent-cli-installation-ticket/v3")
            self.assertEqual(ticket["accounts"], list(claims.accounts))
            self.assertEqual(keychain.values["lulu_agent_cli_installation_ticket_v1"], legacy)
            self.assertEqual(keychain.values["unrelated-user-account"], unknown)
            touched = {key for _operation, key in keychain.calls}
            self.assertNotIn("lulu_agent_cli_installation_ticket_v1", touched)
            self.assertNotIn("unrelated-user-account", touched)

    def test_conflict_is_zero_write_and_partial_write_requires_recovery(self) -> None:
        from src.core.cli_runtime.keychain import (
            VersionedCredentialError,
            provision_versioned_installation_credentials,
        )

        with TemporaryDirectory(prefix="lulu-versioned-failure-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "fixture-nonce-failure")
            claims = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )

            conflict = FakeVersionedKeychain({claims.accounts[1]: "winner"})
            with _patched_test_authority_validation():
                with self.assertRaises(VersionedCredentialError) as existing:
                    provision_versioned_installation_credentials(
                        post_build_receipt=locator,
                        locator_receipt=locator,
                        plan_receipt=plan,
                        keychain=conflict,
                    )
            self.assertEqual(existing.exception.reason_code, "VERSIONED_ACCOUNT_CONFLICT")
            self.assertEqual(conflict.write_order, [])

            partial = FakeVersionedKeychain(fail_write_at=2)
            with _patched_test_authority_validation():
                with self.assertRaises(VersionedCredentialError) as failed:
                    provision_versioned_installation_credentials(
                        post_build_receipt=locator,
                        locator_receipt=locator,
                        plan_receipt=plan,
                        keychain=partial,
                        hmac_key_factory=lambda: b"h" * 32,
                        epoch_factory=lambda: "e" * 32,
                    )
            self.assertEqual(
                failed.exception.reason_code,
                "INSTALLATION_RECOVERY_REQUIRED",
            )
            self.assertEqual(partial.write_order, [claims.accounts[0]])


if __name__ == "__main__":
    unittest.main()
