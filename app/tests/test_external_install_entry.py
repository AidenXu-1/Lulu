"""Entry routing only: no real GUI, user data, Keychain or process launch."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class ExternalInstallEntryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "external_install_entry_test", Path(__file__).resolve().parents[1] / "run_external.py")
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)
        self.gui = Mock(return_value=0)
        self.notice = Mock(return_value=2)
        self.install = Mock(return_value=0)
        self.fake_gui = types.ModuleType("src.main")
        self.fake_gui.main = self.gui
        self.fake_installer = types.ModuleType("src.core.agent_entry.installer_ui")
        self.fake_installer.run_installer = self.install
        self.fake_installer.show_install_location_required = self.notice

    def run_entry(self, executable, *arguments):
        with patch.object(self.entry, "_assert_packaged_profile"), \
                patch.object(sys, "executable", executable), \
                patch.object(sys, "argv", [executable, *arguments]), \
                patch.dict(sys.modules, {"src.main": self.fake_gui,
                    "src.core.agent_entry.installer_ui": self.fake_installer}):
            return self.entry.main()

    def test_gui_location_is_not_redirected_to_a_separate_installer(self):
        # Routing only. This does not certify backend support at these paths.
        for path in ("/Volumes/安装 Lulu/Lulu.app", "/Users/example/Downloads/Lulu.app",
                     "/Users/example/Applications/Lulu.app", "/Applications/Lulu 2.app",
                     "/private/var/folders/example/AppTranslocation/id/d/Lulu.app"):
            with self.subTest(path=path):
                self.assertEqual(self.run_entry(path + "/Contents/MacOS/Lulu"), 0)
                self.gui.assert_called_with()
                self.notice.assert_not_called()
                self.install.assert_not_called()

    def test_canonical_app_opens_normally(self):
        self.assertEqual(self.run_entry("/Applications/Lulu.app/Contents/MacOS/Lulu"), 0)
        self.gui.assert_called_once_with()
        self.notice.assert_not_called()

    def test_staged_installer_still_routes_to_installation(self):
        self.run_entry("/private/tmp/stage/Lulu.app/Contents/MacOS/Lulu",
                       "--install-lulu", "--source-volume", "/Volumes/安装 Lulu")
        self.install.assert_called_once_with(Path("/Volumes/安装 Lulu"))
        self.gui.assert_not_called()
        self.notice.assert_not_called()

    def test_update_helper_is_not_blocked_by_gui_location_check(self):
        self.run_entry("/private/tmp/helper/Lulu.app/Contents/MacOS/Lulu",
                       "--lulu-update-helper", "/private/tmp/plan.json")
        self.gui.assert_called_once_with()
        self.notice.assert_not_called()
