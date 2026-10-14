"""Exercise the GUI/default-factory upgrade with an old installed DMG receipt."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import test_generation9_formal_install_public as fixtures


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

    def __init__(self, root):
        for name in ("last_output_dir", "media_dir", "recording_dir", "tts_output_dir",
                     "voice_library_dir", "models_dir"):
            setattr(self, name, str(root / name))

    def save(self):
        return True


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


@contextmanager
def replaced_installation():
    from src.core.agent_entry.formal_install import build_default_formal_installer
    with TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        old_app, old_dmg, paths, keys, service, installer = fixtures.Generation9FormalInstallPublicTests()._installer(root)
        installer.install(transaction_id="old-install-before-app-replacement")
        shutil.copy2(paths.dmg_receipt, paths.state_root / "verified-dmg.json")
        old_app.rename(root / "previous-app")
        new_app = fixtures._fixture_app(root / "next", source_revision="3" * 40,
                                       skill_text="# replacement bundled Skill\n")
        new_app.rename(old_app)
        new_dmg = root / "new-matching.dmg"
        new_dmg.write_bytes(b"new disk image fixture\n")
        home = root / "home"

        def mapped_path(value):
            return old_app if str(value) == "/Applications/Lulu.app" else Path(value)
        cls = stack.enter_context(patch("src.core.agent_entry.formal_install.Path", side_effect=mapped_path))
        cls.home.return_value = home
        stack.enter_context(patch("src.core.license.store.StrictKeyringStore", return_value=keys))
        stack.enter_context(patch("src.core.agent_entry.effects.CurrentUserLaunchctlService", return_value=service))
        # Only the external disk-image commands are simulated. The public
        # provisioner, filesystem comparisons, factory and transaction are real.
        mounts = []
        def disk_command(args, **kwargs):
            if args[:2] == ["/usr/bin/hdiutil", "verify"]:
                return subprocess.CompletedProcess(args, 0, b"verified", b"")
            if args[:2] == ["/usr/bin/hdiutil", "attach"]:
                mount = Path(args[args.index("-mountpoint") + 1])
                payload = old_app if Path(args[-1]) == new_dmg else root / "previous-app"
                shutil.copytree(payload, mount / "Lulu.app")
                mounts.append(mount)
                return subprocess.CompletedProcess(args, 0, plistlib.dumps({"system-entities": [
                    {"dev-entry": "/dev/disk-fixture", "mount-point": str(mount)}]}), b"")
            if args == ["/sbin/mount"]:
                return subprocess.CompletedProcess(args, 0,
                    f"/dev/disk-fixture on {mounts[-1].resolve()} (apfs, read-only)\n".encode(), b"")
            if args[:2] == ["/usr/bin/hdiutil", "detach"]:
                return subprocess.CompletedProcess(args, 0, b"detached", b"")
            raise AssertionError(f"Unexpected external process: {args}")
        stack.enter_context(patch("src.core.agent_entry.formal_install.subprocess.run", side_effect=disk_command))
        yield root, home, paths, keys, service, old_dmg, new_dmg, build_default_formal_installer


class UpgradeSelectionPublicTests(unittest.TestCase):
    def test_existing_old_dmg_receipt_requires_reselection_after_app_replacement(self):
        from src.core.agent_entry.formal_install import DmgSelectionRequired
        with replaced_installation() as (_, home, _paths, keys, _service, _old, _new, factory):
            before, key_before = snapshot(home), dict(keys.values)
            with self.assertRaises(DmgSelectionRequired):
                factory()
            self.assertEqual(snapshot(home), before)
            self.assertEqual(keys.values, key_before)

    def _page(self, root, factory, selector):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from src.ui.settings_page import SettingsPage
        self.app = QApplication.instance() or QApplication([])
        page = SettingsPage(Config(root), agent_entry_installer_factory=factory,
                            agent_entry_dmg_selector=selector,
                            agent_skill_status_provider=lambda: "update_available")
        from PySide6.QtTest import QTest
        for _ in range(200):
            if page.agent_skill_state != "checking":
                break
            QTest.qWait(5)
        return page

    def test_user_cancels_new_dmg_selection_and_existing_installation_is_untouched(self):
        with replaced_installation() as (root, home, _paths, keys, _service, _old, _new, factory):
            before, key_before = snapshot(home), dict(keys.values)
            selected = []
            def cancel():
                selected.append(True)
                return None
            page = self._page(root, factory, cancel)
            page.agent_skill_install_button.click()
            self.assertEqual(selected, [True])
            self.assertEqual(page.agent_skill_state, "update_available")
            self.assertEqual(snapshot(home), before)
            self.assertEqual(keys.values, key_before)
            page.close()

    def test_user_selects_previous_dmg_and_no_receipt_or_managed_content_changes(self):
        with replaced_installation() as (root, home, _paths, keys, _service, old, _new, factory):
            before, key_before = snapshot(home), dict(keys.values)
            page = self._page(root, factory, lambda: old)
            page.agent_skill_install_button.click()
            self.assertEqual(page.agent_skill_state, "incompatible")
            self.assertEqual(snapshot(home), before)
            self.assertEqual(keys.values, key_before)
            page.close()

    def test_user_selects_matching_dmg_and_same_managed_installation_updates(self):
        from src.core.agent_entry.ownership import verify_ownership_receipt, verify_ready_action_receipt
        with replaced_installation() as (root, home, paths, keys, service, _old, new, factory):
            old_ownership = verify_ownership_receipt(paths.state_root / "ownership.json", key_store=keys)
            page = self._page(root, factory, lambda: new)
            page.agent_skill_install_button.click()
            self.assertEqual(page.agent_skill_state, "ready")
            self.assertEqual((paths.skill_target / "SKILL.md").read_text(), "# replacement bundled Skill\n")
            ownership = verify_ownership_receipt(paths.state_root / "ownership.json", key_store=keys)
            receipt = verify_ready_action_receipt(paths.state_root / "current.json", ownership=ownership)
            self.assertEqual(ownership["installation_id"], old_ownership["installation_id"])
            self.assertEqual(receipt["action"], "entry_update")
            self.assertEqual(factory().status(), "ready")
            page.close()

    def test_damaged_new_app_fails_without_offer_to_reselect_a_dmg(self):
        from src.core.agent_entry.formal_install import DmgSelectionRequired
        with replaced_installation() as (root, home, paths, _keys, _service, _old, _new, factory):
            (paths.app_bundle / "Contents/MacOS/Lulu").write_bytes(b"tampered")
            before = snapshot(home)
            with self.assertRaises(Exception) as error:
                factory()
            self.assertNotIsInstance(error.exception, DmgSelectionRequired)
            self.assertEqual(snapshot(home), before)


if __name__ == "__main__":
    unittest.main()
