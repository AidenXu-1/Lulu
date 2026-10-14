from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_cli_versioned_installation_ticket import (
    FakeVersionedKeychain,
    _authority,
    _reload_test_authority,
)


class InstallationRecoveryTests(unittest.TestCase):
    def test_recovery_result_and_journal_bind_exact_physical_target(self):
        from src.core.cli_runtime.installation_recovery import (
            recover_versioned_installation_credentials,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-target-bound-recovery-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            applications = root / "Applications"
            applications.mkdir(mode=0o700)
            locator, plan = _authority(
                root,
                "target-bound-recovery-nonce",
                installation_target=applications / "Lulu Trusted Test.app",
            )
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            result = recover_versioned_installation_credentials(
                locator_receipt=locator,
                plan_receipt=plan,
                keychain=FakeVersionedKeychain(),
            )
            self.assertEqual(result["target_role"], authority.target_role)
            self.assertEqual(
                result["installation_target"], authority.installation_target
            )
            self.assertEqual(
                result["target_binding_sha256"], authority.target_binding_sha256
            )
            journal = Path(result["journal_path"]).read_text(encoding="utf-8")
            self.assertIn(authority.plan_sha256, journal)
            self.assertNotIn(authority.accounts[0], journal)

    def test_formal_app_snapshot_drift_prevents_keychain_mutation(self):
        from src.core.cli_runtime.installation_recovery import (
            InstallationRecoveryError,
            recover_versioned_installation_credentials,
        )

        with TemporaryDirectory(prefix="lulu-recovery-formal-drift-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            applications = root / "Applications"
            applications.mkdir(mode=0o700)
            locator, plan = _authority(
                root,
                "formal-drift-nonce",
                installation_target=applications / "Lulu Trusted Test.app",
            )
            formal = applications / "Lulu.app/Contents/MacOS"
            formal.mkdir(parents=True, mode=0o700)
            (formal / "Lulu").write_bytes(b"unexpected-formal-app")
            (formal / "Lulu").chmod(0o700)
            for parent in (formal.parent.parent, formal.parent, formal):
                parent.chmod(0o700)
            keychain = FakeVersionedKeychain()
            with self.assertRaises(InstallationRecoveryError) as blocked:
                recover_versioned_installation_credentials(
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=keychain,
                )
            self.assertEqual(
                blocked.exception.reason_code,
                "FORMAL_APP_PROTECTION_DRIFT",
            )
            self.assertEqual(keychain.calls, [])
            self.assertFalse(
                Path(plan.authority.plan_path)
                .with_name("ticket-recovery.jsonl")
                .exists()
            )

    def test_fresh_process_recovers_only_live_owned_prefix_zero_one_two_three(self):
        from src.core.cli_runtime.installation_recovery import (
            recover_versioned_installation_credentials,
        )
        from src.core.cli_runtime import installation_ticket as ownership
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            validate_authority,
        )

        with TemporaryDirectory(prefix="lulu-ticket-recovery-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            applications = root / "Applications"
            applications.mkdir(mode=0o700)
            locator, plan = _authority(
                root,
                "recovery-fixture-nonce",
                installation_target=applications / "Lulu Trusted Test.app",
            )
            authority = validate_authority(locator_receipt=locator, plan_receipt=plan)
            for prefix in range(4):
                with self.subTest(prefix=prefix):
                    keychain = FakeVersionedKeychain(
                        {
                            "lulu_agent_cli_installation_ticket_v1": "legacy-bytes",
                            "unknown-account": "unknown-bytes",
                        }
                    )
                    final_acl = keychain.prepare_final_acl_intent(locator, plan)
                    intents = _new_ownership_intents(authority, final_acl)
                    for index in range(prefix):
                        keychain.create_owned_item(
                            final_acl,
                            intents[index],
                            f"secret-{index}",
                        )
                    ownership._CORE_OWNED_ITEM_REGISTRY.clear()
                    ownership._CORE_OWNERSHIP_INTENT_REGISTRY.clear()
                    ownership._CORE_FINAL_ACL_INTENT_REGISTRY.clear()
                    restarted_locator, restarted_plan = _reload_test_authority(
                        locator, plan
                    )
                    restarted_keychain = FakeVersionedKeychain(
                        state=keychain._memory._state
                    )
                    recovered = recover_versioned_installation_credentials(
                        locator_receipt=restarted_locator,
                        plan_receipt=restarted_plan,
                        keychain=restarted_keychain,
                    )
                    self.assertEqual(recovered["state"], "credentials_recovered")
                    self.assertEqual(restarted_keychain._memory.delete_count, prefix)
                    self.assertTrue(
                        all(
                            account not in restarted_keychain.values
                            for account in authority.accounts
                        )
                    )
                    self.assertEqual(
                        restarted_keychain.values[
                            "lulu_agent_cli_installation_ticket_v1"
                        ],
                        "legacy-bytes",
                    )
                    self.assertEqual(restarted_keychain._memory.secret_read_count, 0)

    def test_foreign_nonprefix_and_proof_drift_fail_before_any_delete(self):
        from src.core.cli_runtime.installation_recovery import (
            InstallationRecoveryError,
            recover_versioned_installation_credentials,
        )
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            validate_authority,
        )

        with TemporaryDirectory(prefix="lulu-proof-drift-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            applications = root / "Applications"
            applications.mkdir(mode=0o700)
            locator, plan = _authority(
                root,
                "proof-drift",
                installation_target=applications / "Lulu Trusted Test.app",
            )
            authority = validate_authority(locator_receipt=locator, plan_receipt=plan)
            for mutation in (
                "foreign",
                "nonprefix",
                "envelope",
                "acl",
                "persistent_ref",
                "attempt_mix",
            ):
                with self.subTest(mutation=mutation):
                    keychain = FakeVersionedKeychain()
                    final_acl = keychain.prepare_final_acl_intent(locator, plan)
                    intents = _new_ownership_intents(authority, final_acl)
                    if mutation == "foreign":
                        keychain.values[authority.accounts[0]] = "preexisting"
                    elif mutation == "nonprefix":
                        keychain.create_owned_item(final_acl, intents[1], "anchor-only")
                    elif mutation == "attempt_mix":
                        keychain.create_owned_item(final_acl, intents[0], "hmac")
                        second_attempt = _new_ownership_intents(authority, final_acl)
                        keychain.create_owned_item(
                            final_acl, second_attempt[1], "anchor"
                        )
                    else:
                        keychain.create_owned_item(final_acl, intents[0], "hmac")
                        item = keychain._memory._owned[authority.accounts[0]]
                        if mutation == "envelope":
                            item["envelope"] = b"{}\n"
                        elif mutation == "acl":
                            item["final_acl_sha256"] = "f" * 64
                        else:
                            item["persistent_ref"] = b"drifted-ref"
                    with self.assertRaises(InstallationRecoveryError) as blocked:
                        recover_versioned_installation_credentials(
                            locator_receipt=locator,
                            plan_receipt=plan,
                            keychain=keychain,
                        )
                    self.assertIn(
                        blocked.exception.reason_code,
                        {
                            "KEYCHAIN_OWNERSHIP_PROOF_REQUIRED",
                            "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR",
                        },
                    )
                    self.assertEqual(keychain._memory.delete_count, 0)
                    self.assertEqual(keychain._memory.secret_read_count, 0)

    def test_plain_data_or_delete_failure_never_reports_recovered(self):
        from src.core.cli_runtime.installation_recovery import (
            InstallationRecoveryError,
            recover_versioned_installation_credentials,
        )

        with TemporaryDirectory(prefix="lulu-ticket-recovery-fail-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            applications = root / "Applications"
            applications.mkdir(mode=0o700)
            locator, plan = _authority(
                root,
                "recovery-failure-nonce",
                installation_target=applications / "Lulu Trusted Test.app",
            )

            class FailingKeychain(FakeVersionedKeychain):
                def delete_verified_item(self, final_acl_intent_receipt, receipt):
                    raise RuntimeError("delete unavailable")

            from src.core.cli_runtime.installation_ticket import (
                _new_ownership_intents,
                validate_authority,
            )
            failing = FailingKeychain()
            authority = validate_authority(locator_receipt=locator, plan_receipt=plan)
            final_acl = failing.prepare_final_acl_intent(locator, plan)
            failing.create_owned_item(
                final_acl,
                _new_ownership_intents(authority, final_acl)[0],
                "partial",
            )

            with self.assertRaises(InstallationRecoveryError) as failed:
                recover_versioned_installation_credentials(
                    locator_receipt=locator,
                    plan_receipt=plan,
                    keychain=failing,
                )
            self.assertEqual(failed.exception.reason_code, "INSTALLATION_RECOVERY_REQUIRED")

            with self.assertRaises((TypeError, InstallationRecoveryError)):
                recover_versioned_installation_credentials(
                    locator_receipt={"fake": True},
                    plan_receipt={"fake": True},
                    keychain=FakeVersionedKeychain(),
                )


if __name__ == "__main__":
    unittest.main()
