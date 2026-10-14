from __future__ import annotations

import ast
import copy
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
import runpy
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from src.core.cli_runtime.contracts import CodeSignatureEvidenceV1
from src.core.cli_runtime.installation_ticket import AuthorityError
from tests.test_cli_versioned_installation_ticket import (
    FakeVersionedKeychain,
    _authority,
    _patched_test_authority_validation,
    _validate_test_authority,
)


class _SignatureVerifier:
    def __init__(self, evidence):
        self.evidence = evidence

    def verify(self, *, app_bundle, cli_path):
        return self.evidence


class _OrderedKeychain(FakeVersionedKeychain):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def write_new(self, key, value):
        super().write_new(key, value)
        self.order.append("write:" + key.split(":", 1)[0])

    def delete_text(self, key, *, missing_ok=False):
        super().delete_text(key, missing_ok=missing_ok)
        self.order.append("delete:" + key.split(":", 1)[0])

    def create_owned_item(
        self, final_acl_intent_receipt, intent_receipt, secret_value
    ):
        receipt = super().create_owned_item(
            final_acl_intent_receipt,
            intent_receipt,
            secret_value,
        )
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        account = _owned_item_claims(receipt).authority.accounts[
            _owned_item_claims(receipt).account_index
        ]
        self.order.append("write:" + account.split(":", 1)[0])
        return receipt

    def delete_verified_item(self, final_acl_intent_receipt, receipt):
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        claims = _owned_item_claims(receipt)
        account = claims.authority.accounts[claims.account_index]
        super().delete_verified_item(final_acl_intent_receipt, receipt)
        self.order.append("delete:" + account.split(":", 1)[0])


class _Acl:
    def __init__(self, order, fail_at=None):
        self.order = order
        self.fail_at = fail_at
        self.count = 0

    def bind_owned_item(self, *, item_receipt, cdhash):
        from src.core.cli_runtime.installation_ticket import _owned_item_claims

        claims = _owned_item_claims(item_receipt)
        account = claims.authority.accounts[claims.account_index]
        self.count += 1
        if self.fail_at == self.count:
            raise RuntimeError("injected ACL failure")
        self.order.append("bind:" + account.split(":", 1)[0])


def _tree_bindings(root: Path) -> tuple[str, str]:
    content_records: list[bytes] = []
    profile_records: list[dict[str, object]] = []

    def visit(parent: Path) -> None:
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                content_records.append(f"D\0{relative}\0{mode:o}\n".encode())
                profile_records.append(
                    {"path": relative, "type": "dir", "mode": mode}
                )
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                content_records.append(
                    f"F\0{relative}\0{mode:o}\0{len(raw)}\0{digest}\n".encode()
                )
                profile_records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "mode": mode,
                        "size": len(raw),
                        "sha256": digest,
                    }
                )
            else:
                raise AssertionError(f"unsupported fixture entry: {relative}")

    visit(root)
    content = hashlib.sha256(b"".join(sorted(content_records))).hexdigest()
    profile = hashlib.sha256(
        json.dumps(
            profile_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return content, profile


def _fixture(root: Path):
    cli_bytes = b"phase10 versioned installed cli\n"
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
        "canonical_cli_sha256": hashlib.sha256(cli_bytes).hexdigest(),
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
        "phase10-versioned-fixture",
        candidate=candidate,
        installation_target=root / "targets/Lulu Trusted Test.app",
    )
    return cli_bytes, signature, locator, plan


@contextmanager
def _phase10_test_authority(install, locator, plan, signature):
    replacements = {
        "load_fixed_post_build_authority": lambda: locator,
        "_fixed_candidate_bundle_for_installer": lambda _receipt: (
            Path(plan.authority.plan_path).parents[5]
            / "candidate/Lulu Trusted Test.app"
        ),
        "_prepare_fixed_candidate_installation": lambda **_kwargs: (locator, plan),
        "validate_authority": _validate_test_authority,
        "MacOSCodeSignatureVerifier": lambda: _SignatureVerifier(signature),
    }
    with _patched_test_authority_validation(), mock.patch.dict(
        install.__globals__, replacements
    ):
        yield


class Phase10VersionedTicketInstallTests(unittest.TestCase):
    @staticmethod
    def _namespace():
        path = Path(__file__).resolve().parents[1] / "packaging/phase10_install_versioned_ticket.py"
        return runpy.run_path(str(path))

    def test_phase10_public_entry_has_no_caller_install_target_mapping(self):
        public = self._namespace()["install_versioned_ticket"]
        parameters = inspect.signature(public).parameters
        self.assertNotIn("install_arguments", parameters)
        for forbidden in (
            "candidate_bundle",
            "app_target",
            "resource_root",
            "cli_path",
            "app_bundle",
            "target_path",
            "profile_override",
            "frozen_installer_loader",
            "hmac_key_factory",
            "epoch_factory",
            "after_write",
        ):
            self.assertNotIn(forbidden, parameters)

    def test_production_has_no_custom_authority_or_caller_receipt_composition(self):
        app_root = Path(__file__).resolve().parents[1]
        ticket_path = app_root / "src/core/cli_runtime/installation_ticket.py"
        phase10_path = app_root / "packaging/phase10_install_versioned_ticket.py"
        factory_path = app_root / "src/core/cli_runtime/factory.py"
        forbidden_ticket_symbols = {
            "CandidateEvidenceReader",
            "FrozenCandidateVerifier",
            "_issue_phase10_frozen_receipts",
            "_register_locator",
            "_register_measured_evidence",
            "_register_plan",
            "persist_plan_before_keychain_write",
        }
        forbidden_phase10_symbols = {"_install_versioned_ticket_with_receipts"}

        def defined_symbols(path: Path) -> set[str]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            return {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            }

        self.assertTrue(forbidden_ticket_symbols.isdisjoint(defined_symbols(ticket_path)))
        self.assertTrue(forbidden_phase10_symbols.isdisjoint(defined_symbols(phase10_path)))
        factory_source = factory_path.read_text(encoding="utf-8")
        for forbidden in (
            "FrozenCandidateVerifier",
            "evidence_reader",
            "expected_candidate",
            "nonce_factory",
        ):
            self.assertNotIn(forbidden, factory_source)
        public = self._namespace()["install_versioned_ticket"]
        self.assertTrue(
            {
                "locator_receipt",
                "plan_receipt",
                "signature_verifier",
                "candidate_evidence",
            }.isdisjoint(inspect.signature(public).parameters)
        )

    def test_public_orchestrator_rejects_candidate_evidence_and_signature_verifier(self):
        install_versioned_ticket = self._namespace()["install_versioned_ticket"]

        parameters = inspect.signature(install_versioned_ticket).parameters
        self.assertNotIn("candidate_evidence", parameters)
        self.assertNotIn("signature_verifier", parameters)
        for injected in (
            {"candidate_evidence": Path("/tmp/caller-evidence.json")},
            {"signature_verifier": object()},
            {"locator_receipt": {"token": "caller-duck"}},
            {"plan_receipt": {"sha256": "0" * 64}},
        ):
            keychain = FakeVersionedKeychain()
            with self.assertRaises(TypeError):
                install_versioned_ticket(
                    **injected,
                    keychain=keychain,
                    acl_boundary=object(),
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                )
            self.assertEqual(keychain.calls, [])
        keychain = FakeVersionedKeychain()
        with self.assertRaises(TypeError):
            install_versioned_ticket(
                candidate_evidence=Path("/tmp/caller-evidence.json"),
                keychain=keychain,
                acl_boundary=object(),
                hmac_key_factory=lambda: b"h" * 32,
                epoch_factory=lambda: "e" * 32,
            )
        self.assertEqual(keychain.calls, [])

    def test_public_orchestrator_has_no_caller_verifier_parameter(self):
        install_versioned_ticket = self._namespace()["install_versioned_ticket"]

        self.assertNotIn(
            "verifier",
            inspect.signature(install_versioned_ticket).parameters,
        )
        keychain = FakeVersionedKeychain()
        with self.assertRaises(TypeError):
            install_versioned_ticket(
                verifier=object(),
                keychain=keychain,
                acl_boundary=object(),
                hmac_key_factory=lambda: b"h" * 32,
                epoch_factory=lambda: "e" * 32,
            )
        self.assertEqual(keychain.calls, [])

    def test_whole_binding_self_selection_mints_no_plan_or_keychain_effect(self):
        namespace = self._namespace()
        install_versioned_ticket = namespace["install_versioned_ticket"]
        Phase10VersionedTicketError = namespace["Phase10VersionedTicketError"]

        with TemporaryDirectory(prefix="lulu-phase10-old-self-select-") as directory:
            root = Path(directory).resolve()
            home = root / "home"
            home.mkdir(mode=0o700)
            bundle = root / "Self Selected.app"
            bundle.mkdir()
            keychain = FakeVersionedKeychain()
            installer_loads = []
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaises(TypeError):
                    install_versioned_ticket(
                        candidate_bundle=bundle,
                        keychain=keychain,
                        acl_boundary=_Acl([]),
                        frozen_installer_loader=lambda: installer_loads.append(True),
                        hmac_key_factory=lambda: b"h" * 32,
                        epoch_factory=lambda: "e" * 32,
                    )
            self.assertEqual(keychain.calls, [])
            self.assertEqual(installer_loads, [])
            self.assertEqual(list(root.rglob("ticket-plan.json")), [])

    def test_home_env_cannot_select_authoritative_plan_root(self):
        import pwd

        import src.core.cli_runtime.installation_ticket as ticket
        from tests import test_phase10_post_build_authority as post_build_tests

        with TemporaryDirectory(prefix="lulu-phase10-home-authority-") as directory:
            root = Path(directory).resolve()
            system_home = root / "system-home"
            attacker_home = root / "attacker-home"
            for home in (system_home, attacker_home):
                support = home / "Library/Application Support"
                support.mkdir(parents=True, mode=0o700)
                for path in (home, home / "Library", support):
                    os.chmod(path, 0o700)
            attacker_before = tuple(attacker_home.rglob("*"))
            helper = post_build_tests.Phase10PostBuildAuthorityTests()
            try:
                _authority, _build, _produced, _finalized, post_build = (
                    helper._issue_chain(root)
                )
                with (
                    mock.patch.dict(os.environ, {"HOME": str(attacker_home)}),
                    mock.patch.object(
                        pwd,
                        "getpwuid",
                        return_value=SimpleNamespace(
                            pw_dir=str(system_home), pw_uid=os.getuid()
                        ),
                    ),
                    mock.patch.object(
                        ticket,
                        "_protected_formal_app_snapshot",
                        return_value={"state": "absent"},
                    ),
                ):
                    locator, _plan = ticket._prepare_fixed_candidate_installation(
                        post_build_receipt=post_build
                    )
                public = ticket.locator_public_fingerprint(locator)
                self.assertTrue(
                    Path(public["plan_path"]).is_relative_to(
                        system_home / "Library/Application Support"
                    )
                )
                self.assertEqual(tuple(attacker_home.rglob("*")), attacker_before)
            finally:
                helper.doCleanups()

    def test_system_account_home_anomalies_fail_before_first_plan_write(self):
        import pwd

        import src.core.cli_runtime.installation_ticket as ticket
        from tests import test_phase10_post_build_authority as post_build_tests

        def tree_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
            return tuple(
                (path.relative_to(root).as_posix(), os.readlink(path) if path.is_symlink() else "")
                for path in sorted(root.rglob("*"))
            )

        with TemporaryDirectory(prefix="lulu-phase10-home-fail-closed-") as directory:
            root = Path(directory).resolve()
            attacker_home = root / "attacker-home"
            (attacker_home / "Library/Application Support").mkdir(
                parents=True, mode=0o700
            )
            for path in (
                attacker_home,
                attacker_home / "Library",
                attacker_home / "Library/Application Support",
            ):
                os.chmod(path, 0o700)
            missing_home = root / "missing-home"
            file_home = root / "file-home"
            file_home.write_text("not a directory", encoding="utf-8")
            valid_home = root / "valid-home"
            (valid_home / "Library/Application Support").mkdir(
                parents=True, mode=0o700
            )
            for path in (
                valid_home,
                valid_home / "Library",
                valid_home / "Library/Application Support",
            ):
                os.chmod(path, 0o700)
            linked_home = root / "linked-home"
            linked_home.symlink_to(valid_home, target_is_directory=True)
            linked_library_home = root / "linked-library-home"
            linked_library_home.mkdir(mode=0o700)
            (linked_library_home / "Library").symlink_to(
                valid_home / "Library", target_is_directory=True
            )
            linked_support_home = root / "linked-support-home"
            (linked_support_home / "Library").mkdir(parents=True, mode=0o700)
            (linked_support_home / "Library/Application Support").symlink_to(
                valid_home / "Library/Application Support", target_is_directory=True
            )
            helper = post_build_tests.Phase10PostBuildAuthorityTests()
            try:
                _authority, _build, _produced, _finalized, post_build = (
                    helper._issue_chain(root)
                )
                before = tree_snapshot(root)
                registry_sizes = (
                    len(ticket._CORE_LOCATOR_REGISTRY),
                    len(ticket._CORE_PLAN_REGISTRY),
                )
                cases = (
                    ("relative", "relative-home", os.getuid()),
                    ("nul-byte", "/tmp/invalid\0home", os.getuid()),
                    ("missing", str(missing_home), os.getuid()),
                    ("file", str(file_home), os.getuid()),
                    ("home-symlink", str(linked_home), os.getuid()),
                    ("library-symlink", str(linked_library_home), os.getuid()),
                    ("support-symlink", str(linked_support_home), os.getuid()),
                    ("account-uid-mismatch", str(valid_home), os.getuid() + 1),
                )
                with mock.patch.dict(os.environ, {"HOME": str(attacker_home)}):
                    for label, raw_home, account_uid in cases:
                        with self.subTest(label=label):
                            with mock.patch.object(
                                pwd,
                                "getpwuid",
                                return_value=SimpleNamespace(
                                    pw_dir=raw_home, pw_uid=account_uid
                                ),
                            ):
                                with self.assertRaises(AuthorityError) as blocked:
                                    ticket._prepare_fixed_candidate_installation(
                                        post_build_receipt=post_build
                                    )
                            self.assertEqual(
                                blocked.exception.reason_code,
                                "INSTALLATION_PLAN_INTEGRITY_ERROR",
                            )
                            self.assertEqual(tree_snapshot(root), before)
                            self.assertEqual(
                                (
                                    len(ticket._CORE_LOCATOR_REGISTRY),
                                    len(ticket._CORE_PLAN_REGISTRY),
                                ),
                                registry_sizes,
                            )
            finally:
                helper.doCleanups()

    def test_same_claim_non_cli_drift_mints_no_receipt_plan_or_external_effect(self):
        namespace = self._namespace()
        Phase10VersionedTicketError = namespace["Phase10VersionedTicketError"]
        install_versioned_ticket = namespace["install_versioned_ticket"]
        with TemporaryDirectory(prefix="lulu-phase10-self-select-") as directory:
            root = Path(directory).resolve()
            bundle = root / "Lulu Trusted Test.app"
            bundle.mkdir()
            keychain = FakeVersionedKeychain()
            installer_loads = []
            with mock.patch.dict(
                install_versioned_ticket.__globals__,
                {
                    "load_fixed_post_build_authority": lambda: (_ for _ in ()).throw(
                        AuthorityError("POST_BUILD_AUTHORITY_INTEGRITY_ERROR")
                    )
                },
            ):
                with self.assertRaises(Phase10VersionedTicketError) as blocked:
                    install_versioned_ticket(
                        keychain=keychain,
                        acl_boundary=_Acl([]),
                    )
            self.assertEqual(blocked.exception.reason_code, "POST_BUILD_AUTHORITY_INTEGRITY_ERROR")
            self.assertEqual(keychain.calls, [])
            self.assertEqual(installer_loads, [])
            self.assertEqual(list(root.rglob("ticket-plan.json")), [])

    def test_plan_install_hmac_anchor_ticket_atomic_final_acl_order(self):
        install_versioned_ticket = self._namespace()[
            "_install_versioned_ticket_transaction"
        ]

        with TemporaryDirectory(prefix="lulu-phase10-versioned-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            cli_bytes, signature, locator, plan = _fixture(root)
            app = root / "targets/Lulu Trusted Test.app"
            order = []

            def install_candidate(*, locator_receipt, plan_receipt):
                self.assertIs(locator_receipt, locator)
                self.assertIs(plan_receipt, plan)
                self.assertTrue(any((root / "Application Support").rglob("ticket-plan.json")))
                order.append("install")
                cli = app / "Contents/MacOS/lulu-agent-cli"
                cli.parent.mkdir(parents=True)
                cli.write_bytes(cli_bytes)
                cli.chmod(0o700)
                return {"state": "installed"}

            def rollback_install(*, locator_receipt, plan_receipt):
                order.append("rollback")
                return {"state": "rolled_back"}

            with _phase10_test_authority(
                install_versioned_ticket, locator, plan, signature
            ):
                result = install_versioned_ticket(
                    keychain=_OrderedKeychain(order),
                    acl_boundary=_Acl(order),
                    frozen_installer_loader=lambda: {
                        "install_candidate": install_candidate,
                        "rollback_install": rollback_install,
                    },
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                )

            self.assertEqual(result["state"], "installed_versioned_ticket")
            self.assertEqual(
                order,
                [
                    "install",
                    "write:lulu_agent_cli_job_hmac_key_v1",
                    "write:lulu_agent_cli_job_anchor_v1",
                    "write:lulu_agent_cli_installation_ticket_v3",
                ],
            )

    def test_credential_failure_recovers_before_file_rollback(self):
        namespace = self._namespace()
        Phase10VersionedTicketError = namespace["Phase10VersionedTicketError"]
        install_versioned_ticket = namespace[
            "_install_versioned_ticket_transaction"
        ]

        with TemporaryDirectory(prefix="lulu-phase10-recover-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            cli_bytes, signature, locator, plan = _fixture(root)
            app = root / "targets/Lulu Trusted Test.app"
            order = []

            def install_candidate(*, locator_receipt, plan_receipt):
                order.append("install")
                cli = app / "Contents/MacOS/lulu-agent-cli"
                cli.parent.mkdir(parents=True)
                cli.write_bytes(cli_bytes)
                cli.chmod(0o700)
                return {"state": "installed"}

            def rollback_install(*, locator_receipt, plan_receipt):
                order.append("rollback")
                return {"state": "rolled_back"}

            with _phase10_test_authority(
                install_versioned_ticket, locator, plan, signature
            ):
                with self.assertRaises(Phase10VersionedTicketError) as failed:
                    install_versioned_ticket(
                        keychain=_OrderedKeychain(order),
                        acl_boundary=_Acl(order),
                        frozen_installer_loader=lambda: {
                            "install_candidate": install_candidate,
                            "rollback_install": rollback_install,
                        },
                        hmac_key_factory=lambda: b"h" * 32,
                        epoch_factory=lambda: "e" * 32,
                        after_write=lambda count: (_ for _ in ()).throw(RuntimeError())
                        if count == 1
                        else None,
                    )
            self.assertEqual(failed.exception.reason_code, "INSTALLATION_RECOVERY_REQUIRED")
            self.assertEqual(
                [entry for entry in order if entry.startswith("delete:")],
                ["delete:lulu_agent_cli_job_hmac_key_v1"],
            )
            self.assertEqual(order[-1], "rollback")

    def test_preexisting_conflict_at_each_index_rolls_back_files_without_credential_delete(self):
        namespace = self._namespace()
        install_versioned_ticket = namespace["_install_versioned_ticket_transaction"]
        error_type = namespace["Phase10VersionedTicketError"]

        for conflict_index in range(3):
            with self.subTest(conflict_index=conflict_index), TemporaryDirectory(
                prefix="lulu-phase10-preexisting-"
            ) as directory:
                root = Path(directory).resolve()
                os.chmod(root, 0o700)
                cli_bytes, signature, locator, plan = _fixture(root)
                authority = _validate_test_authority(
                    locator_receipt=locator, plan_receipt=plan
                )
                formal_app = root / "targets/Lulu.app"
                self.assertFalse(formal_app.exists())
                keychain = _OrderedKeychain([])
                preexisting = {
                    "value": "preexisting-owner-value",
                    "attributes": "foreign-attributes",
                    "acl": "foreign-acl",
                    "persistent_ref": b"foreign-ref-" + bytes([conflict_index]),
                }
                keychain.values[authority.accounts[conflict_index]] = preexisting["value"]
                keychain._memory._owned[authority.accounts[conflict_index]] = {
                    "envelope": b'{"foreign":true}\n',
                    "persistent_ref": preexisting["persistent_ref"],
                    "attributes_sha256": preexisting["attributes"],
                    "acl_policy_sha256": preexisting["acl"],
                }
                state_before = copy.deepcopy(keychain._memory._state)
                order: list[str] = []

                def install_candidate(**_kwargs):
                    order.append("install")
                    cli = root / "targets/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli"
                    cli.parent.mkdir(parents=True)
                    cli.write_bytes(cli_bytes)
                    cli.chmod(0o700)
                    return {"state": "installed"}

                def rollback_install(**_kwargs):
                    order.append("rollback")
                    return {"state": "rolled_back"}

                with _phase10_test_authority(
                    install_versioned_ticket, locator, plan, signature
                ):
                    with self.assertRaises(error_type) as blocked:
                        install_versioned_ticket(
                            keychain=keychain,
                            acl_boundary=_Acl(order),
                            frozen_installer_loader=lambda: {
                                "install_candidate": install_candidate,
                                "rollback_install": rollback_install,
                            },
                            hmac_key_factory=lambda: b"h" * 32,
                            epoch_factory=lambda: "e" * 32,
                        )
                self.assertEqual(blocked.exception.reason_code, "VERSIONED_ACCOUNT_CONFLICT")
                self.assertEqual(order, ["install", "rollback"])
                self.assertEqual(keychain._memory.create_count, 0)
                self.assertEqual(keychain._memory.delete_count, 0)
                self.assertEqual(
                    keychain.values[authority.accounts[conflict_index]],
                    preexisting["value"],
                )
                self.assertEqual(keychain._memory._state, state_before)
                self.assertFalse(formal_app.exists())

    def test_owned_proof_drift_blocks_credential_delete_and_file_rollback(self):
        namespace = self._namespace()
        install_versioned_ticket = namespace["_install_versioned_ticket_transaction"]
        error_type = namespace["Phase10VersionedTicketError"]
        with TemporaryDirectory(prefix="lulu-phase10-proof-drift-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            cli_bytes, signature, locator, plan = _fixture(root)
            authority = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            order: list[str] = []
            keychain = _OrderedKeychain(order)

            def install_candidate(**_kwargs):
                order.append("install")
                cli = root / "targets/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli"
                cli.parent.mkdir(parents=True)
                cli.write_bytes(cli_bytes)
                cli.chmod(0o700)
                return {"state": "installed"}

            def rollback_install(**_kwargs):
                order.append("rollback")
                return {"state": "rolled_back"}

            def corrupt_then_crash(count):
                if count == 1:
                    keychain._memory._owned[authority.accounts[0]]["envelope"] = b"{}\n"
                    raise RuntimeError("hard crash after owned write")

            with _phase10_test_authority(
                install_versioned_ticket, locator, plan, signature
            ):
                with self.assertRaises(error_type) as blocked:
                    install_versioned_ticket(
                        keychain=keychain,
                        acl_boundary=_Acl(order),
                        frozen_installer_loader=lambda: {
                            "install_candidate": install_candidate,
                            "rollback_install": rollback_install,
                        },
                        hmac_key_factory=lambda: b"h" * 32,
                        epoch_factory=lambda: "e" * 32,
                        after_write=corrupt_then_crash,
                    )
            self.assertEqual(
                blocked.exception.reason_code,
                "KEYCHAIN_OWNERSHIP_PROOF_REQUIRED",
            )
            self.assertEqual(keychain._memory.delete_count, 0)
            self.assertNotIn("rollback", order)

    def test_isolated_real_adapter_live_drift_blocks_delete_and_file_rollback(self):
        from tests.test_license_keychain_store import _isolated_macos_owned_backend

        namespace = self._namespace()
        install_versioned_ticket = namespace["_install_versioned_ticket_transaction"]
        error_type = namespace["Phase10VersionedTicketError"]
        mutations = {
            "service": lambda item: item.__setitem__("service", "Foreign"),
            "account": lambda item: item.__setitem__("account", "foreign-account"),
            "persistent_ref": lambda item: item.__setitem__(
                "reported_persistent_ref", b"drifted-persistent-ref"
            ),
            "partition_acl": lambda item: (
                item.__setitem__("partition_ids", ("cdhash:" + "b" * 40,)),
                item.__setitem__("evidence_sha256", "f" * 64),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(drift=name), TemporaryDirectory(
                prefix="lulu-phase10-real-adapter-drift-"
            ) as directory:
                root = Path(directory).resolve()
                os.chmod(root, 0o700)
                cli_bytes, signature, locator, plan = _fixture(root)
                authority = _validate_test_authority(
                    locator_receipt=locator,
                    plan_receipt=plan,
                )
                order: list[str] = []
                keychain = _isolated_macos_owned_backend()

                def install_candidate(**_kwargs):
                    order.append("install")
                    cli = root / "targets/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli"
                    cli.parent.mkdir(parents=True)
                    cli.write_bytes(cli_bytes)
                    cli.chmod(0o700)
                    return {"state": "installed"}

                def rollback_install(**_kwargs):
                    order.append("rollback")
                    return {"state": "rolled_back"}

                def drift_then_crash(count):
                    if count == 1:
                        mutate(keychain.items[authority.accounts[0]])
                        raise RuntimeError("hard crash after live item drift")

                with _phase10_test_authority(
                    install_versioned_ticket,
                    locator,
                    plan,
                    signature,
                ):
                    with self.assertRaises(error_type):
                        install_versioned_ticket(
                            keychain=keychain,
                            acl_boundary=_Acl(order),
                            frozen_installer_loader=lambda: {
                                "install_candidate": install_candidate,
                                "rollback_install": rollback_install,
                            },
                            hmac_key_factory=lambda: b"h" * 32,
                            epoch_factory=lambda: "e" * 32,
                            after_write=drift_then_crash,
                        )
                self.assertEqual(keychain.delete_count, 0)
                self.assertNotIn("rollback", order)
                self.assertFalse((root / "targets/Lulu.app").exists())

    def test_legacy_post_create_acl_boundary_is_never_invoked(self):
        namespace = self._namespace()
        install_versioned_ticket = namespace["_install_versioned_ticket_transaction"]
        with TemporaryDirectory(prefix="lulu-phase10-no-post-bind-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            cli_bytes, signature, locator, plan = _fixture(root)
            order: list[str] = []

            def install_candidate(**_kwargs):
                order.append("install")
                cli = root / "targets/Lulu Trusted Test.app/Contents/MacOS/lulu-agent-cli"
                cli.parent.mkdir(parents=True)
                cli.write_bytes(cli_bytes)
                cli.chmod(0o700)
                return {"state": "installed"}

            with _phase10_test_authority(
                install_versioned_ticket, locator, plan, signature
            ):
                result = install_versioned_ticket(
                    keychain=_OrderedKeychain(order),
                    acl_boundary=_Acl(order, fail_at=1),
                    frozen_installer_loader=lambda: {
                        "install_candidate": install_candidate,
                        "rollback_install": lambda **_kwargs: {
                            "state": "rolled_back"
                        },
                    },
                    hmac_key_factory=lambda: b"h" * 32,
                    epoch_factory=lambda: "e" * 32,
                )
            self.assertEqual(result["state"], "installed_versioned_ticket")
            self.assertFalse(any(item.startswith("bind:") for item in order))


if __name__ == "__main__":
    unittest.main()
