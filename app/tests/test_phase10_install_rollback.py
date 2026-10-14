"""Phase 10 owner-safe candidate installation and byte-exact rollback tests."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import replace
from pathlib import Path
import plistlib
import runpy
import shlex
import shutil
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import inspect

from tests.test_cli_versioned_installation_ticket import (
    _TEST_AUTHORITY_SEAL,
    _TestLocatorReceipt,
    _TestPlanReceipt,
    _authority,
    _validate_test_authority,
)


_OWNER = "lulu.phase10.install-candidate/v1"


class _FakeAdHocCodesign:
    def __call__(self, argv: list[str]) -> SimpleNamespace:
        if argv[1:4] == ["--verify", "--deep", "--strict"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["--display", "--verbose=4"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(
                    f"Executable={argv[-1]}\n"
                    "Identifier=com.lulu.transcribe.trusted-test\n"
                    "Signature=adhoc\n"
                    "TeamIdentifier=not set\n"
                ),
            )
        raise AssertionError(f"unexpected codesign invocation: {argv!r}")


class _RejectAdHocCodesign:
    def __call__(self, argv: list[str]) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid signature")


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)


def _candidate_bundle(root: Path) -> Path:
    app_root = Path(__file__).resolve().parents[1]
    bundle = root / "source candidate" / "Lulu-Trusted-Test.app"
    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    frameworks = bundle / "Contents" / "Frameworks"
    macos.mkdir(parents=True, mode=0o700)
    resources.mkdir(mode=0o700)
    frameworks.mkdir(mode=0o700)
    for path in (bundle, bundle / "Contents", macos, resources, frameworks):
        os.chmod(path, 0o700)
    _write(macos / "Lulu-Trusted-Test", b"candidate-gui", 0o700)
    _write(macos / "lulu-agent-cli", b"candidate-cli", 0o700)
    _write(frameworks / "shared-python-runtime", b"single-runtime")
    _write(
        bundle / "Contents" / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                "CFBundleExecutable": "Lulu-Trusted-Test",
                "CFBundleShortVersionString": "0.3.2",
                "CFBundleVersion": "20260823.1",
                "LuluBuildProfile": "trusted_test",
                "LuluBuildIdentity": "trusted-test",
            }
        ),
    )
    _write(
        resources / "agent_product_manifest.json",
        (app_root / "packaging" / "agent_product_manifest.json").read_bytes(),
    )
    _write(
        resources / "build_profile.json",
        (
            json.dumps(
                {
                    "schema": 1,
                    "profile": "trusted_test",
                    "version": "0.3.2",
                    "build": "20260823.1",
                    "source_commit": "1" * 40,
                    "source_tree": "2" * 40,
                    "product_source_sha256": "3" * 64,
                    "app_name": "Lulu Trusted Test",
                    "bundle_id": "com.lulu.transcribe.trusted-test",
                    "artifact_prefix": "Lulu-Trusted-Test",
                    "upload_target": "none",
                    "entry": "run_trusted_test.py",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    )
    shutil.copytree(
        app_root / "skills" / "lulu-agent",
        resources / "skills" / "lulu-agent",
    )
    return bundle


def _old_app(path: Path, marker: bytes) -> None:
    _write(
        path / "Contents" / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                "CFBundleShortVersionString": "0.3.1",
                "CFBundleVersion": "20260815.3",
            }
        ),
    )
    _write(path / "Contents" / "MacOS" / "Lulu", marker, 0o700)
    for item in (path, path / "Contents", path / "Contents" / "MacOS"):
        os.chmod(item, 0o700)


def _formal_app(path: Path, marker: bytes) -> None:
    _write(
        path / "Contents/Info.plist",
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lulu.transcribe",
                "CFBundleShortVersionString": "0.3.1",
                "CFBundleVersion": "20260815.3",
            }
        ),
    )
    _write(path / "Contents/MacOS/Lulu", marker, 0o700)
    for item in (path, path / "Contents", path / "Contents/MacOS"):
        os.chmod(item, 0o700)


def _managed_launcher(path: Path, marker: str) -> None:
    _write(
        path,
        (
            "#!/bin/sh\n"
            "# lulu.phase10-managed-launcher/v1\n"
            f"# owner_tool={_OWNER}\n"
            f"# marker={marker}\n"
            "exit 91\n"
        ).encode("utf-8"),
        0o700,
    )


def _managed_skill(path: Path, marker: str) -> None:
    path.mkdir(parents=True, mode=0o700)
    _write(path / "SKILL.md", f"old-skill-{marker}\n".encode("utf-8"))
    _write(
        path / ".lulu-phase10-owner.json",
        (json.dumps({"owner": _OWNER}) + "\n").encode("utf-8"),
    )


def _snapshot(path: Path) -> dict[str, tuple[str, bytes | str, int]]:
    result: dict[str, tuple[str, bytes | str, int]] = {}
    if not path.exists() and not path.is_symlink():
        return result
    pending = [path]
    while pending:
        current = pending.pop()
        relative = "." if current == path else current.relative_to(path).as_posix()
        metadata = current.lstat()
        mode = metadata.st_mode & 0o7777
        if current.is_symlink():
            result[relative] = ("symlink", os.readlink(current), mode)
        elif current.is_dir():
            result[relative] = ("directory", "", mode)
            pending.extend(sorted(current.iterdir(), reverse=True))
        else:
            result[relative] = ("file", current.read_bytes(), mode)
    return result


def _prepare(root: Path, *, formal_marker: bytes | None = None) -> dict[str, Path]:
    app_root = Path(__file__).resolve().parents[1]
    bundle = _candidate_bundle(root)
    evidence_dir = root / "candidate evidence"
    evidence_dir.mkdir(mode=0o700)
    evidence = evidence_dir / "candidate.json"
    finalizer = runpy.run_path(
        str(app_root / "packaging" / "phase10_finalize_candidate.py")
    )
    finalizer["finalize_candidate"](
        bundle=bundle,
        bundle_role="dist",
        output_path=evidence,
        codesign_runner=_FakeAdHocCodesign(),
    )
    evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))

    applications = root / "Applications"
    bin_root = root / ".local/bin"
    skill_root = root / ".agents/skills"
    support = root / "Library/Application Support"
    user_data = root / "共享用户数据"
    for directory in (applications, bin_root, skill_root, support, user_data):
        directory.mkdir(mode=0o700, parents=True)
        os.chmod(directory, 0o700)
    if formal_marker is not None:
        _formal_app(applications / "Lulu.app", formal_marker)
    candidate = {
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "product_version": "0.3.2",
        "build": "20260823.1",
        "source_commit": "1" * 40,
        "source_tree": "2" * 40,
        "app_content_tree_sha256": evidence_payload["bundle_tree_sha256"],
        "app_profile_tree_sha256": "4" * 64,
        "canonical_cli_sha256": hashlib.sha256(
            (bundle / "Contents/MacOS/lulu-agent-cli").read_bytes()
        ).hexdigest(),
        "signature": {
            "signature_kind": "ad_hoc",
            "certificate_sha256": "",
            "cdhash": "5" * 40,
            "designated_requirement": f'cdhash H"{"5" * 40}"',
            "team_id": "",
        },
    }
    locator, plan = _authority(
        support,
        candidate=candidate,
        installation_target=applications / "Lulu Trusted Test.app",
    )
    journal_path = Path(plan.authority.plan_path).with_name(
        "install-transaction.json"
    )
    return {
        "candidate_bundle": bundle,
        "candidate_evidence": evidence,
        "skill_source": bundle
        / "Contents"
        / "Resources"
        / "skills"
        / "lulu-agent",
        "app_target": applications / "Lulu Trusted Test.app",
        "formal_app": applications / "Lulu.app",
        "launcher_target": bin_root / "lulu",
        "skill_target": skill_root / "lulu-agent",
        "journal_path": journal_path,
        "locator_receipt": locator,
        "plan_receipt": plan,
        "user_data": user_data,
    }


def _install_arguments(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "locator_receipt": paths["locator_receipt"],
        "plan_receipt": paths["plan_receipt"],
        "candidate_bundle": paths["candidate_bundle"],
        "candidate_evidence": paths["candidate_evidence"],
        "codesign_runner": _FakeAdHocCodesign(),
    }


class Phase10InstallRollbackTests(unittest.TestCase):
    @staticmethod
    def _namespace() -> dict[str, object]:
        app_root = Path(__file__).resolve().parents[1]
        installer = app_root / "packaging" / "phase10_install_candidate.py"
        if not installer.is_file():
            raise AssertionError("Phase 10 recoverable installer is missing")
        namespace = runpy.run_path(str(installer))
        for name in (
            "_install_candidate_transaction",
            "rollback_install",
        ):
            namespace[name].__globals__["validate_authority"] = (
                _validate_test_authority
            )
        return namespace

    def test_public_file_transaction_accepts_authority_not_caller_targets(self) -> None:
        namespace = self._namespace()
        install_parameters = inspect.signature(namespace["install_candidate"]).parameters
        rollback_parameters = inspect.signature(namespace["rollback_install"]).parameters
        for forbidden in (
            "app_target",
            "backup_root",
            "journal_path",
            "target_path",
            "destination",
            "profile_override",
        ):
            self.assertNotIn(forbidden, install_parameters)
            self.assertNotIn(forbidden, rollback_parameters)
        self.assertTrue({"locator_receipt", "plan_receipt"}.issubset(install_parameters))
        self.assertEqual(set(rollback_parameters), {"locator_receipt", "plan_receipt"})

    def test_install_then_rollback_restores_app_launcher_and_skill_bytes(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        rollback = namespace["rollback_install"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-success")
            _managed_launcher(paths["launcher_target"], "success")
            _managed_skill(paths["skill_target"], "success")
            _write(paths["user_data"] / "models" / "weight.bin", b"user-model")
            _write(paths["user_data"] / "transcripts" / "draft.md", b"user-draft")
            before = {
                key: _snapshot(paths[key])
                for key in ("app_target", "launcher_target", "skill_target", "user_data")
            }

            result = install(**_install_arguments(paths))
            self.assertEqual(result["state"], "installed")
            self.assertEqual(result["effect_scope"], "explicit_local_install_targets")
            self.assertTrue(paths["journal_path"].is_file())
            self.assertEqual(
                (
                    paths["app_target"]
                    / "Contents"
                    / "MacOS"
                    / "Lulu-Trusted-Test"
                ).read_bytes(),
                b"candidate-gui",
            )
            launcher = paths["launcher_target"].read_text(encoding="utf-8")
            canonical_cli = paths["app_target"] / "Contents" / "MacOS" / "lulu-agent-cli"
            self.assertIn("# lulu.phase10-managed-launcher/v1", launcher)
            self.assertIn(f"# owner_tool={_OWNER}", launcher)
            self.assertIn(f"exec {shlex.quote(str(canonical_cli))} \"$@\"", launcher)
            self.assertNotIn(str(paths["candidate_bundle"]), launcher)
            self.assertEqual(
                json.loads(
                    (paths["skill_target"] / ".lulu-phase10-owner.json").read_text(
                        encoding="utf-8"
                    )
                )["owner"],
                _OWNER,
            )
            journal = json.loads(paths["journal_path"].read_text(encoding="utf-8"))
            self.assertEqual(journal["state"], "installed")
            for target in journal["targets"]:
                self.assertIn("original_manifest", target)
                self.assertIn("installed_manifest", target)
                self.assertTrue(Path(target["backup_path"]).exists())
            self.assertEqual(_snapshot(paths["user_data"]), before["user_data"])

            rolled_back = rollback(
                locator_receipt=paths["locator_receipt"],
                plan_receipt=paths["plan_receipt"],
            )
            self.assertEqual(rolled_back["state"], "rolled_back")
            for key in ("app_target", "launcher_target", "skill_target", "user_data"):
                self.assertEqual(_snapshot(paths[key]), before[key], key)

    def test_target_binding_namespace_and_formal_app_stay_byte_exact(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        rollback = namespace["rollback_install"]
        with tempfile.TemporaryDirectory(prefix="Phase10 target binding ") as directory:
            paths = _prepare(
                Path(directory).resolve(),
                formal_marker=b"formal-protected",
            )
            formal_before = _snapshot(paths["formal_app"])
            result = install(**_install_arguments(paths))
            journal = json.loads(paths["journal_path"].read_text(encoding="utf-8"))
            binding = result["target_binding_sha256"]
            by_key = {item["key"]: item for item in journal["targets"]}
            self.assertEqual(
                Path(by_key["app"]["stage_path"]),
                paths["app_target"].parent
                / f".Lulu Trusted Test.app.{binding}.stage",
            )
            self.assertEqual(
                Path(by_key["app"]["backup_path"]),
                paths["app_target"].parent
                / f".Lulu Trusted Test.app.{binding}.backup",
            )
            for key in ("launcher", "skill"):
                self.assertIn(binding, Path(by_key[key]["stage_path"]).name)
                self.assertIn(binding, Path(by_key[key]["backup_path"]).name)
            self.assertEqual(_snapshot(paths["formal_app"]), formal_before)
            rollback(
                locator_receipt=paths["locator_receipt"],
                plan_receipt=paths["plan_receipt"],
            )
            self.assertEqual(_snapshot(paths["formal_app"]), formal_before)

    def test_cross_profile_path_variants_and_same_object_fail_before_journal(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 target rejects ") as directory:
            paths = _prepare(Path(directory).resolve())
            locator = paths["locator_receipt"]
            plan = paths["plan_receipt"]
            base = locator.authority

            cases = []
            cross_candidate = dict(base.candidate)
            cross_candidate["profile"] = "external_candidate"
            cases.append(replace(base, candidate=cross_candidate))
            for name in (
                "lulu Trusted Test.app",
                "Lulu Trusted Test\N{NARROW NO-BREAK SPACE}.app",
            ):
                target = paths["app_target"].with_name(name)
                candidate = dict(base.candidate)
                candidate["installation_target"] = str(target)
                cases.append(
                    replace(
                        base,
                        installation_target=str(target),
                        candidate=candidate,
                    )
                )

            for authority in cases:
                with self.subTest(target=authority.installation_target):
                    forged_locator = _TestLocatorReceipt(
                        _TEST_AUTHORITY_SEAL, authority
                    )
                    forged_plan = _TestPlanReceipt(
                        _TEST_AUTHORITY_SEAL,
                        authority,
                        plan.plan_bytes,
                    )
                    arguments = _install_arguments(paths)
                    arguments.update(
                        locator_receipt=forged_locator,
                        plan_receipt=forged_plan,
                    )
                    with self.assertRaises(CandidateInstallError):
                        install(**arguments)
                    self.assertFalse(paths["journal_path"].exists())

            install.__globals__["_same_object"] = lambda _left, _right: True
            try:
                with self.assertRaises(CandidateInstallError):
                    install(**_install_arguments(paths))
            finally:
                install.__globals__["_same_object"] = namespace["_same_object"]
            self.assertFalse(paths["journal_path"].exists())

    def test_invalid_candidate_or_foreign_targets_cause_zero_mutation(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-invalid")
            _managed_launcher(paths["launcher_target"], "invalid")
            _managed_skill(paths["skill_target"], "invalid")
            before = {
                key: _snapshot(paths[key])
                for key in ("app_target", "launcher_target", "skill_target")
            }
            _write(
                paths["candidate_bundle"]
                / "Contents"
                / "MacOS"
                / "lulu-agent-cli",
                b"tampered-after-finalize",
                0o700,
            )
            with self.assertRaises(CandidateInstallError):
                install(**_install_arguments(paths))
            self.assertFalse(paths["journal_path"].exists())
            self.assertFalse(paths["journal_path"].exists())
            for key in before:
                self.assertEqual(_snapshot(paths[key]), before[key])

        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _write(paths["launcher_target"], b"#!/bin/sh\n# competitor\n", 0o700)
            _managed_skill(paths["skill_target"], "foreign")
            before_launcher = _snapshot(paths["launcher_target"])
            with self.assertRaises(CandidateInstallError):
                install(**_install_arguments(paths))
            self.assertEqual(_snapshot(paths["launcher_target"]), before_launcher)
            self.assertFalse(paths["journal_path"].exists())
            self.assertFalse(paths["journal_path"].exists())

    def test_install_rechecks_adhoc_signature_before_any_mutation(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-signature")
            _managed_launcher(paths["launcher_target"], "signature")
            _managed_skill(paths["skill_target"], "signature")
            before = {
                key: _snapshot(paths[key])
                for key in ("app_target", "launcher_target", "skill_target")
            }

            with self.assertRaises(CandidateInstallError):
                arguments = _install_arguments(paths)
                arguments["codesign_runner"] = _RejectAdHocCodesign()
                install(
                    **arguments,
                )

            self.assertFalse(paths["journal_path"].exists())
            for key in before:
                self.assertEqual(_snapshot(paths[key]), before[key], key)

    def test_exact_system_applications_root_accepts_only_admin_member_owner_target(self) -> None:
        namespace = self._namespace()
        target_parent = namespace["_target_parent"]
        CandidateInstallError = namespace["CandidateInstallError"]
        exact_target = Path("/Applications/Lulu Trusted Test.app")

        def metadata(*, kind: str, uid: int, gid: int, mode: int) -> SimpleNamespace:
            kind_mode = stat.S_IFDIR if kind == "directory" else stat.S_IFLNK
            return SimpleNamespace(st_mode=kind_mode | mode, st_uid=uid, st_gid=gid)

        def validate(
            *,
            applications_mode: int = 0o775,
            applications_kind: str = "directory",
            executing_uid: int = 501,
            groups: tuple[int, ...] = (20, 80),
            target_metadata: SimpleNamespace | None = None,
            target: Path = exact_target,
        ) -> Path:
            mapping = {
                Path("/"): metadata(kind="directory", uid=0, gid=0, mode=0o755),
                Path("/Applications"): metadata(
                    kind=applications_kind,
                    uid=0,
                    gid=80,
                    mode=applications_mode,
                ),
            }

            def fake_lstat(path: Path) -> SimpleNamespace:
                normalized = Path(path)
                if normalized == exact_target:
                    if target_metadata is None:
                        raise FileNotFoundError(str(normalized))
                    return target_metadata
                if normalized in mapping:
                    return mapping[normalized]
                raise FileNotFoundError(str(normalized))

            with patch.object(Path, "lstat", fake_lstat), patch(
                "os.getuid", return_value=executing_uid
            ), patch("os.getgid", return_value=20), patch(
                "os.getgroups", return_value=list(groups)
            ), patch(
                "grp.getgrnam", return_value=SimpleNamespace(gr_gid=80)
            ):
                return target_parent(target, "Applications target")

        self.assertEqual(validate(), exact_target)
        self.assertEqual(
            validate(
                target_metadata=metadata(
                    kind="directory", uid=501, gid=20, mode=0o700
                )
            ),
            exact_target,
        )
        rejected = (
            {"applications_mode": 0o777},
            {"applications_kind": "symlink"},
            {"groups": (20,)},
            {"executing_uid": 0, "groups": (0, 80)},
            {
                "target_metadata": metadata(
                    kind="directory", uid=502, gid=20, mode=0o700
                )
            },
            {
                "target_metadata": metadata(
                    kind="symlink", uid=501, gid=20, mode=0o700
                )
            },
            {"target": Path("/Applications/nested/Lulu Trusted Test.app")},
            {
                "target": Path(
                    "/Applications/../Applications/Lulu Trusted Test.app"
                )
            },
        )
        for case in rejected:
            with self.subTest(case=case), self.assertRaises(CandidateInstallError):
                validate(**case)

    def test_fault_is_automatically_restored_and_symlink_or_relative_input_is_rejected(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-fault")
            _managed_launcher(paths["launcher_target"], "fault")
            _managed_skill(paths["skill_target"], "fault")
            _write(paths["user_data"] / "voices" / "voice.wav", b"user-voice")
            before = {
                key: _snapshot(paths[key])
                for key in ("app_target", "launcher_target", "skill_target", "user_data")
            }
            with self.assertRaises(CandidateInstallError):
                install(
                    **_install_arguments(paths),
                    fault_after="launcher",
                )
            for key in before:
                self.assertEqual(_snapshot(paths[key]), before[key], key)
            journal = json.loads(paths["journal_path"].read_text(encoding="utf-8"))
            self.assertEqual(journal["state"], "failed_restored")
            self.assertIs(journal["all_originals_backed_up"], True)
            self.assertFalse(
                any("phase10-stage" in item.name for item in paths["app_target"].parent.iterdir())
            )

        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            linked = Path(directory).resolve() / "Linked.app"
            linked.symlink_to(paths["candidate_bundle"], target_is_directory=True)
            rejected = _install_arguments(paths)
            rejected["candidate_bundle"] = linked
            with self.assertRaises(CandidateInstallError):
                install(**rejected)
            self.assertFalse(paths["journal_path"].exists())
            relative = dict(rejected)
            relative["candidate_bundle"] = Path("Lulu.app")
            with self.assertRaises(CandidateInstallError):
                install(**relative)

    def test_staging_fault_is_journaled_without_touching_any_target(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-stage-fault")
            _managed_launcher(paths["launcher_target"], "stage-fault")
            _managed_skill(paths["skill_target"], "stage-fault")
            before = {
                key: _snapshot(paths[key])
                for key in ("app_target", "launcher_target", "skill_target")
            }
            with self.assertRaises(CandidateInstallError):
                install(
                    **_install_arguments(paths),
                    fault_after="stage_app",
                )
            for key in before:
                self.assertEqual(_snapshot(paths[key]), before[key], key)
            journal = json.loads(paths["journal_path"].read_text(encoding="utf-8"))
            self.assertEqual(journal["state"], "failed_restored")
            self.assertFalse(
                any("phase10-stage" in item.name for item in paths["app_target"].parent.iterdir())
            )

    def test_tampered_journal_cannot_redirect_rollback_into_user_data(self) -> None:
        namespace = self._namespace()
        install = namespace["_install_candidate_transaction"]
        rollback = namespace["rollback_install"]
        CandidateInstallError = namespace["CandidateInstallError"]
        with tempfile.TemporaryDirectory(prefix="Phase10 中文 空格 ") as directory:
            paths = _prepare(Path(directory).resolve())
            _old_app(paths["app_target"], b"old-gui-journal")
            _managed_launcher(paths["launcher_target"], "journal")
            _managed_skill(paths["skill_target"], "journal")
            install(**_install_arguments(paths))
            redirected = paths["user_data"] / "Lulu.app"
            shutil.copytree(paths["app_target"], redirected, symlinks=True)
            before_user_data = _snapshot(paths["user_data"])
            journal = json.loads(paths["journal_path"].read_text(encoding="utf-8"))
            journal["targets"][0]["target_path"] = str(redirected)
            paths["journal_path"].write_text(
                json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(paths["journal_path"], 0o600)

            with self.assertRaises(CandidateInstallError):
                rollback(
                    locator_receipt=paths["locator_receipt"],
                    plan_receipt=paths["plan_receipt"],
                )
            self.assertEqual(_snapshot(paths["user_data"]), before_user_data)


if __name__ == "__main__":
    unittest.main()
