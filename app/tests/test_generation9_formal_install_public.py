from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from src.core.agent_entry.component_manifest import build_component_manifest
from src.core.agent_entry.ownership import (
    verify_ownership_receipt,
    verify_ready_action_receipt,
)


SOURCE_REVISION = "2" * 40


class MemoryKeys:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_text(self, key: str) -> str | None:
        return self.values.get(key)

    def write_new(self, key: str, value: str) -> None:
        if key in self.values:
            raise RuntimeError("existing key")
        self.values[key] = value

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        if key not in self.values and not missing_ok:
            raise RuntimeError("missing key")
        self.values.pop(key, None)


class MemoryService:
    def __init__(self) -> None:
        self.loaded = False
        self.starts = 0
        self.stops = 0

    def is_loaded(self) -> bool:
        return self.loaded

    def start(self, _plist: Path) -> None:
        self.loaded = True
        self.starts += 1

    def stop(self) -> None:
        self.loaded = False
        self.stops += 1


def _fixture_app(
    root: Path,
    *,
    product_build: str = "20260904.1",
    source_revision: str = SOURCE_REVISION,
    skill_text: str = "# Lulu Agent\n",
) -> Path:
    app = root / "Lulu.app"
    resources = app / "Contents/Resources"
    macos = app / "Contents/MacOS"
    launch_agents = app / "Contents/Library/LaunchAgents"
    skill = resources / "skills/lulu-agent"
    for directory in (macos, launch_agents, skill):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("Lulu", "lulu-agent-cli", "lulu-agent-core"):
        path = macos / name
        path.write_bytes(f"fixture:{name}\n".encode())
        path.chmod(0o755)
    (resources / "LuluTransportPolicy.v1.plist").write_bytes(
        plistlib.dumps(
            {
                "Schema": "lulu.agent-xpc-transport/v1",
                "CandidateGeneration": 9,
                "MachService": "com.lulu.transcribe.agent-core",
                "Scope": "current_user_gui_session",
                "NetworkListener": False,
                "SocketFallback": False,
                "PathFallback": False,
            }
        )
    )
    (resources / "build_profile.json").write_text(
        json.dumps(
            {
                "profile": "external_candidate",
                "version": "0.3.2",
                "build": product_build,
                "source_commit": source_revision,
            }
        ),
        encoding="utf-8",
    )
    catalog = resources / "src/agent_cli/capability_catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {"schema_id": "lulu.agent-capability-catalog/v1", "version": "1.0.0"}
        ),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (skill / "manifest.json").write_text(
        json.dumps(
            {
                "schema_id": "lulu.agent-skill-manifest/v1",
                "skill_version": "1.1.0",
                "candidate_generation": 9,
                "product_identity": {
                    "product_version": "0.3.2",
                    "product_build": product_build,
                    "source_revision": source_revision,
                    "installation_profile": "circle_internal",
                },
                "cli_protocol": {"minimum": "1.0.0", "maximum_exclusive": "2.0.0"},
                "catalog_version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    (launch_agents / "com.lulu.transcribe.agent-core.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.lulu.transcribe.agent-core",
                "MachServices": {"com.lulu.transcribe.agent-core": True},
                "ProgramArguments": [
                    "/Applications/Lulu.app/Contents/MacOS/lulu-agent-core"
                ],
            }
        )
    )
    manifest_path = resources / "agent_product_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    manifest = build_component_manifest(
        app_bundle=app,
        manifest_path=manifest_path,
        product_version="0.3.2",
        product_build=product_build,
        source_revision=source_revision,
        candidate_generation=9,
        cli_protocol_min="1.0.0",
        cli_protocol_max="2.0.0",
        catalog_relative_path="Contents/Resources/src/agent_cli/capability_catalog.json",
        capability_closed_set=("doctor", "transcribe", "version"),
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return app


def _verified_dmg_receipt(
    root: Path,
    app: Path,
    *,
    product_build: str = "20260904.1",
) -> tuple[Path, Path]:
    from src.core.agent_entry.component_manifest import verify_component_manifest

    root.mkdir(parents=True, exist_ok=True)
    dmg = root / f"Lulu-0.3.2-{product_build}-arm64.dmg"
    dmg.write_bytes(b"exact-real-dmg-file-bytes\n")
    metadata = dmg.lstat()
    verification = verify_component_manifest(
        app, app / "Contents/Resources/agent_product_manifest.json"
    )
    manifest_raw = (app / "Contents/Resources/agent_product_manifest.json").read_bytes()
    receipt = root / "verified-dmg.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_id": "lulu.generation9-dmg-verification/v1",
                "verified": True,
                "release_dmg_sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
                "release_dmg_size": metadata.st_size,
                "release_dmg_identity": {
                    "path": str(dmg),
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "mode": f"{metadata.st_mode & 0o777:04o}",
                    "owner": metadata.st_uid,
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                },
                "expected_app": str(app),
                "app_component_set_digest": verification["app_component_set_digest"],
                "component_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "product_version": verification["product_version"],
                "product_build": verification["product_build"],
                "source_revision": verification["source_revision"],
                "candidate_generation": 9,
                "mounted_readonly": True,
                "detached": True,
                "contains_secret_material": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    return dmg, receipt


class Generation9FormalInstallPublicTests(unittest.TestCase):
    def _installer(self, root: Path):
        from src.core.agent_entry.formal_install import (
            FormalAgentEntryInstaller,
            FormalInstallPaths,
        )

        app = _fixture_app(root / "candidate")
        dmg, dmg_receipt = _verified_dmg_receipt(root, app)
        paths = FormalInstallPaths(
            app_bundle=app,
            dmg=dmg,
            dmg_receipt=dmg_receipt,
            state_root=root / "home/Library/Application Support/Lulu/AgentEntry",
            launch_agent_target=root / "home/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist",
            skill_target=root / "home/.agents/skills/lulu-agent",
            launcher_target=root / "home/.local/bin/lulu",
        )
        keys = MemoryKeys()
        service = MemoryService()
        installer = FormalAgentEntryInstaller(
            paths,
            key_store=keys,
            service=service,
            installation_id_factory=lambda: "installation-public-0001",
        )
        return app, dmg, paths, keys, service, installer

    def test_public_install_creates_exact_three_effects_and_ready_receipts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, dmg, paths, keys, service, installer = self._installer(root)
            state = paths.state_root

            result = installer.install(transaction_id="install-public-0001")

            self.assertEqual(result["phase"], "COMMITTED")
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["effect_count"], 3)
            self.assertEqual(service.starts, 1)
            self.assertEqual(
                paths.launch_agent_target.read_bytes(),
                (app / "Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist").read_bytes(),
            )
            self.assertEqual(
                (paths.skill_target / "SKILL.md").read_text(encoding="utf-8"),
                "# Lulu Agent\n",
            )
            self.assertEqual(
                paths.launcher_target.read_text(encoding="utf-8"),
                "#!/bin/sh\n# lulu.agent-launcher/v1\nset -eu\n"
                "exec /Applications/Lulu.app/Contents/MacOS/lulu-agent-cli \"$@\"\n",
            )
            ownership = verify_ownership_receipt(state / "ownership.json", key_store=keys)
            self.assertEqual(ownership["release_dmg_sha256"], hashlib.sha256(dmg.read_bytes()).hexdigest())
            current = verify_ready_action_receipt(state / "current.json", ownership=ownership)
            self.assertEqual(current["binding"], result["binding"])
            action = json.loads(
                (state / "receipts/install-public-0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(action["state"], "ready")
            self.assertEqual(action["effect_count"], 3)

    def test_same_bundled_skill_reinstall_is_a_zero_effect_idempotent_success(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, service, installer = self._installer(root)
            installer.install(transaction_id="install-idempotent-first")
            managed_paths = (
                paths.launch_agent_target,
                paths.skill_target,
                paths.launcher_target,
                paths.state_root / "ownership.json",
            )
            before = {
                path: (path.lstat().st_ino, path.lstat().st_mtime_ns)
                for path in managed_paths
            }
            key_snapshot = dict(keys.values)

            repeated = installer.install(transaction_id="install-idempotent-repeat")

            self.assertEqual(repeated["phase"], "COMMITTED")
            self.assertEqual(repeated["state"], "ready")
            self.assertEqual(repeated["effect_count"], 0)
            self.assertEqual(service.starts, 1)
            self.assertEqual(keys.values, key_snapshot)
            self.assertEqual(
                {
                    path: (path.lstat().st_ino, path.lstat().st_mtime_ns)
                    for path in managed_paths
                },
                before,
            )

    def test_app_update_replaces_only_the_receipted_bundle_with_the_new_bundled_skill(self) -> None:
        from src.core.agent_entry.formal_install import FormalAgentEntryInstaller

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, service, old_installer = self._installer(root)
            old_installer.install(transaction_id="install-before-app-update")
            old_ownership = verify_ownership_receipt(
                paths.state_root / "ownership.json",
                key_store=keys,
            )

            new_app = _fixture_app(
                root / "updated-candidate",
                product_build="20260905.1",
                source_revision="3" * 40,
                skill_text="# Lulu Agent updated\n",
            )
            new_dmg, new_dmg_receipt = _verified_dmg_receipt(
                root / "updated-release",
                new_app,
                product_build="20260905.1",
            )
            updated_paths = type(paths)(
                app_bundle=new_app,
                dmg=new_dmg,
                dmg_receipt=new_dmg_receipt,
                state_root=paths.state_root,
                launch_agent_target=paths.launch_agent_target,
                skill_target=paths.skill_target,
                launcher_target=paths.launcher_target,
            )
            new_installer = FormalAgentEntryInstaller(
                updated_paths,
                key_store=keys,
                service=service,
                installation_id_factory=lambda: "must-not-create-a-second-installation",
            )

            self.assertEqual(old_installer.status(), "ready")
            self.assertEqual(new_installer.status(), "update_available")
            updated = new_installer.install(transaction_id="update-after-app-update")

            self.assertEqual(updated["action"], "entry_update")
            self.assertEqual(updated["state"], "ready")
            self.assertEqual(
                (paths.skill_target / "SKILL.md").read_text(encoding="utf-8"),
                "# Lulu Agent updated\n",
            )
            self.assertEqual(service.starts, 1)
            new_ownership = verify_ownership_receipt(
                paths.state_root / "ownership.json",
                key_store=keys,
            )
            self.assertEqual(
                new_ownership["installation_id"],
                old_ownership["installation_id"],
            )
            self.assertNotEqual(
                new_ownership["component_manifest_sha256"],
                old_ownership["component_manifest_sha256"],
            )

    def test_interrupted_app_update_restores_the_complete_previous_working_version(self) -> None:
        from src.core.agent_entry.formal_install import FormalAgentEntryInstaller
        from src.core.agent_entry.transaction import CrashInjected

        for crash_after in ("skill:effect", "ownership:effect"):
            with self.subTest(crash_after=crash_after), TemporaryDirectory() as temporary:
                root = Path(temporary)
                _app, _dmg, paths, keys, service, old_installer = self._installer(root)
                old_installer.install(transaction_id="install-before-update-crash")
                old_skill = (paths.skill_target / "SKILL.md").read_bytes()
                old_ownership = (paths.state_root / "ownership.json").read_bytes()
                old_current = (paths.state_root / "current.json").read_bytes()

                new_app = _fixture_app(
                    root / "crashing-update-candidate",
                    product_build="20260905.2",
                    source_revision="4" * 40,
                    skill_text="# Lulu Agent update interrupted\n",
                )
                new_dmg, new_dmg_receipt = _verified_dmg_receipt(
                    root / "crashing-update-release",
                    new_app,
                    product_build="20260905.2",
                )
                updated_paths = type(paths)(
                    app_bundle=new_app,
                    dmg=new_dmg,
                    dmg_receipt=new_dmg_receipt,
                    state_root=paths.state_root,
                    launch_agent_target=paths.launch_agent_target,
                    skill_target=paths.skill_target,
                    launcher_target=paths.launcher_target,
                )
                new_installer = FormalAgentEntryInstaller(
                    updated_paths,
                    key_store=keys,
                    service=service,
                    installation_id_factory=lambda: "must-not-create-a-second-installation",
                )
                transaction_id = f"update-crash-{crash_after.replace(':', '-')}"

                with self.assertRaises(CrashInjected):
                    new_installer.install(
                        transaction_id=transaction_id,
                        crash_after=crash_after,
                    )
                recovered = new_installer.recover(transaction_id=transaction_id)

                self.assertEqual(recovered["state"], "rollback_complete")
                self.assertEqual(
                    (paths.skill_target / "SKILL.md").read_bytes(), old_skill
                )
                self.assertEqual(
                    (paths.state_root / "ownership.json").read_bytes(),
                    old_ownership,
                )
                self.assertEqual(
                    (paths.state_root / "current.json").read_bytes(),
                    old_current,
                )
                self.assertTrue(service.loaded)

    def test_each_effect_interruption_recovers_without_unknown_overwrite(self) -> None:
        from src.core.agent_entry.transaction import CrashInjected

        for effect_name in ("core_registration", "skill", "launcher"):
            with self.subTest(effect=effect_name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                _app, _dmg, paths, keys, service, installer = self._installer(root)
                transaction_id = f"install-crash-{effect_name}"

                with self.assertRaises(CrashInjected):
                    installer.install(
                        transaction_id=transaction_id,
                        crash_after=f"{effect_name}:effect",
                    )
                recovered = installer.recover(transaction_id=transaction_id)

                self.assertEqual(recovered["state"], "rollback_complete")
                self.assertFalse(paths.launch_agent_target.exists())
                self.assertFalse(paths.skill_target.exists())
                self.assertFalse(paths.launcher_target.exists())
                self.assertFalse(service.loaded)
                self.assertFalse((paths.state_root / "ownership.json").exists())
                self.assertEqual(keys.values, {})

    def test_ownership_created_before_commit_recovers_file_and_exact_key(self) -> None:
        from src.core.agent_entry.transaction import CrashInjected

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, service, installer = self._installer(root)
            transaction_id = "install-crash-after-ownership"

            with self.assertRaises(CrashInjected):
                installer.install(
                    transaction_id=transaction_id,
                    crash_after="ownership:effect",
                )
            self.assertTrue((paths.state_root / "ownership.json").exists())
            self.assertEqual(len(keys.values), 1)

            recovered = installer.recover(transaction_id=transaction_id)

            self.assertEqual(recovered["state"], "rollback_complete")
            self.assertFalse(paths.launch_agent_target.exists())
            self.assertFalse(paths.skill_target.exists())
            self.assertFalse(paths.launcher_target.exists())
            self.assertFalse(service.loaded)
            self.assertFalse((paths.state_root / "ownership.json").exists())
            self.assertEqual(keys.values, {})

    def test_ownership_cleanup_failure_is_preserved_not_reported_complete(self) -> None:
        from src.core.agent_entry.transaction import CrashInjected

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, _service, installer = self._installer(root)
            transaction_id = "install-ownership-cleanup-failure"
            with self.assertRaises(CrashInjected):
                installer.install(
                    transaction_id=transaction_id,
                    crash_after="ownership:effect",
                )

            real_delete = keys.delete_text

            def reject_delete(_key: str, *, missing_ok: bool = False) -> None:
                raise RuntimeError(f"delete rejected; missing_ok={missing_ok}")

            keys.delete_text = reject_delete  # type: ignore[method-assign]
            recovered = installer.recover(transaction_id=transaction_id)
            current = json.loads(
                (paths.state_root / "current.json").read_text(encoding="utf-8")
            )

            self.assertEqual(recovered["phase"], "RECOVERY_PRESERVED")
            self.assertEqual(recovered["state"], "rollback_incomplete")
            self.assertEqual(current["phase"], "RECOVERY_PRESERVED")
            self.assertEqual(current["state"], "rollback_incomplete")
            self.assertTrue((paths.state_root / "ownership.json").exists())
            self.assertEqual(len(keys.values), 1)

            keys.delete_text = real_delete  # type: ignore[method-assign]
            resolved = installer.recover(transaction_id=transaction_id)
            action = json.loads(
                (paths.state_root / f"receipts/{transaction_id}.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(resolved["state"], "rollback_complete")
            self.assertEqual(action["state"], "rollback_complete")
            self.assertFalse((paths.state_root / "ownership.json").exists())
            self.assertEqual(keys.values, {})

    def test_third_party_skill_conflict_has_zero_managed_effects(self) -> None:
        from src.core.agent_entry.transaction import TransactionError

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, service, installer = self._installer(root)
            paths.skill_target.mkdir(parents=True)
            foreign = paths.skill_target / "foreign.txt"
            foreign.write_text("keep me\n", encoding="utf-8")

            with self.assertRaises(TransactionError) as caught:
                installer.install(transaction_id="install-third-party-skill")

            self.assertEqual(caught.exception.state, "ownership_conflict")
            self.assertEqual(caught.exception.effect_count, 0)
            self.assertEqual(foreign.read_text(encoding="utf-8"), "keep me\n")
            self.assertFalse(paths.launch_agent_target.exists())
            self.assertFalse(paths.launcher_target.exists())
            self.assertEqual(service.starts, 0)
            self.assertFalse((paths.state_root / "ownership.json").exists())
            self.assertEqual(keys.values, {})

    def test_bound_dmg_one_byte_change_is_rejected_before_all_effects(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, dmg, paths, keys, service, installer = self._installer(root)
            with dmg.open("ab") as stream:
                stream.write(b"x")

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_HASH_MISMATCH"):
                installer.install(transaction_id="install-mutated-dmg")

            self.assertFalse(paths.launch_agent_target.exists())
            self.assertFalse(paths.skill_target.exists())
            self.assertFalse(paths.launcher_target.exists())
            self.assertEqual(service.starts, 0)
            self.assertFalse((paths.state_root / "ownership.json").exists())
            self.assertEqual(keys.values, {})

    def test_version_mismatch_stops_before_effects_and_install_never_opens_network(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, keys, service, installer = self._installer(root)
            receipt = json.loads(paths.dmg_receipt.read_text(encoding="utf-8"))
            receipt["product_build"] = "incompatible-build"
            paths.dmg_receipt.write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            paths.dmg_receipt.chmod(0o600)

            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "AGENT_ENTRY_COMPONENT_MISMATCH",
                ):
                    installer.install(transaction_id="install-version-mismatch")

            self.assertFalse(paths.launch_agent_target.exists())
            self.assertFalse(paths.skill_target.exists())
            self.assertFalse(paths.launcher_target.exists())
            self.assertEqual(service.starts, 0)
            self.assertEqual(keys.values, {})

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _app, _dmg, paths, _keys, _service, installer = self._installer(root)
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                installed = installer.install(transaction_id="install-offline-bundled-skill")
            self.assertEqual(installed["state"], "ready")
            self.assertTrue(paths.skill_target.is_dir())

    def test_settings_skill_button_accepts_idempotent_zero_effect_success(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from src.ui.settings_page import SettingsPage

        class Config:
            theme = "warm"
            auto_update_enabled = True
            notification_policy = "never"
            notify_on_failure = True
            notification_sound_enabled = True
            prevent_sleep_during_tasks = False
            confirm_exit_during_tasks = True
            completion_action = "stay"
            save_result = True

            def __init__(self, root: Path) -> None:
                self.last_output_dir = str(root / "exports")
                self.media_dir = str(root / "media")
                self.recording_dir = str(root / "recordings")
                self.tts_output_dir = str(root / "voices")
                self.voice_library_dir = str(root / "voice-library")
                self.models_dir = str(root / "models")

            def save(self) -> bool:
                return True

        class Installer:
            calls: list[str] = []

            def install(self, *, transaction_id: str) -> dict[str, object]:
                self.calls.append(transaction_id)
                return {
                    "phase": "COMMITTED",
                    "state": "ready",
                    "effect_count": 0,
                    "binding": {
                        "release_dmg_sha256": "a" * 64,
                        "component_manifest_sha256": "b" * 64,
                        "app_component_set_digest": "c" * 64,
                        "installation_id_digest": "d" * 64,
                        "candidate_generation": 9,
                    },
                }

        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as temporary:
            installer = Installer()
            page = SettingsPage(
                Config(Path(temporary)),
                agent_entry_installer_factory=lambda: installer,
            )
            from PySide6.QtTest import QTest
            for _ in range(200):
                if page.agent_skill_state != "checking":
                    break
                QTest.qWait(5)
            page.agent_skill_install_button.click()
            app.processEvents()

            self.assertEqual(page.current_category, "general")
            self.assertEqual(len(installer.calls), 1)
            self.assertTrue(installer.calls[0].startswith("agent-install-"))
            self.assertEqual(page.agent_skill_state, "ready")
            self.assertEqual(
                page.agent_skill_status_label.text(),
                "已安装，适配当前 Lulu",
            )
            self.assertTrue(page.agent_skill_install_button.isHidden())

    def test_settings_missing_receipt_selects_original_dmg_then_installs(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from src.core.agent_entry.formal_install import DmgSelectionRequired
        from src.ui.settings_page import SettingsPage

        class Config:
            theme = "warm"
            auto_update_enabled = True
            notification_policy = "never"
            notify_on_failure = True
            notification_sound_enabled = True
            prevent_sleep_during_tasks = False
            confirm_exit_during_tasks = True
            completion_action = "stay"
            save_result = True

            def __init__(self, root: Path) -> None:
                self.last_output_dir = str(root / "exports")
                self.media_dir = str(root / "media")
                self.recording_dir = str(root / "recordings")
                self.tts_output_dir = str(root / "voices")
                self.voice_library_dir = str(root / "voice-library")
                self.models_dir = str(root / "models")

            def save(self) -> bool:
                return True

        class Installer:
            def install(self, *, transaction_id: str) -> dict[str, object]:
                return {
                    "phase": "COMMITTED",
                    "state": "ready",
                    "effect_count": 3,
                    "binding": {
                        "release_dmg_sha256": "a" * 64,
                        "component_manifest_sha256": "b" * 64,
                        "app_component_set_digest": "c" * 64,
                        "installation_id_digest": "d" * 64,
                        "candidate_generation": 9,
                    },
                }

        class Factory:
            def __init__(self, selected: Path) -> None:
                self.selected = selected
                self.calls: list[Path | None] = []

            def __call__(self, dmg: Path | None = None):
                self.calls.append(dmg)
                if dmg is None:
                    raise DmgSelectionRequired()
                if dmg != self.selected:
                    raise RuntimeError("wrong selected DMG")
                return Installer()

        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "Lulu-0.3.2-20260823.4-arm64.dmg"
            selected.write_bytes(b"selected by user")
            factory = Factory(selected)
            selections: list[Path] = []

            def select_dmg() -> Path:
                selections.append(selected)
                return selected

            page = SettingsPage(
                Config(root),
                agent_entry_installer_factory=factory,
                agent_entry_dmg_selector=select_dmg,
            )
            from PySide6.QtTest import QTest
            for _ in range(200):
                if page.agent_skill_state != "checking":
                    break
                QTest.qWait(5)
            page.agent_skill_install_button.click()
            app.processEvents()

            self.assertEqual(selections, [selected])
            self.assertEqual(factory.calls, [None, selected])
            self.assertEqual(page.agent_skill_state, "ready")
            self.assertEqual(
                page.agent_skill_status_label.text(),
                "已安装，适配当前 Lulu",
            )
            self.assertTrue(page.agent_skill_install_button.isHidden())


if __name__ == "__main__":
    unittest.main()
