import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from src.core.license.models import LicenseStatus
from src.ui import theme as T
from src.ui.license_contact import CONTACT_QR_SHA256, LicenseContactDialog, _load_qr_pixmap
from src.ui.license_gate import LicenseGateWindow


class _StubService:
    def __init__(self):
        self.activate_calls = []

    def startup_status(self):
        return LicenseStatus(False, "not_activated", "请输入授权码。")

    def activate(self, code):
        self.activate_calls.append(code)
        return LicenseStatus(False, "invalid", "授权码无效,请检查后重试。")


class LicenseContactDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        T.apply_theme("warm")
        self.asset = Path(__file__).resolve().parents[1] / "src" / "ui" / "assets" / "license_contact_wechat.png"

    def test_asset_bytes_sha_dimensions_and_qr_ratio_are_frozen(self):
        data = self.asset.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), CONTACT_QR_SHA256)
        pixmap = QPixmap(str(self.asset))
        self.assertFalse(pixmap.isNull())
        self.assertEqual((pixmap.width(), pixmap.height()), (280, 262))

        dialog = LicenseContactDialog()
        shown = dialog.qr_image.pixmap()
        logical = shown.deviceIndependentSize()
        self.assertTrue(dialog.qr_available)
        self.assertLessEqual(logical.width(), 224)
        self.assertLessEqual(logical.height(), 210)
        self.assertAlmostEqual(logical.width() / logical.height(), 140 / 131, places=2)
        self.assertFalse(dialog.qr_image.hasScaledContents())
        dialog.close()

    def test_dialog_copy_size_theme_and_focus_contract(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                T.apply_theme(theme_name)
                dialog = LicenseContactDialog()
                dialog.show()
                self.app.processEvents()
                self.assertEqual((dialog.width(), dialog.height()), (388, 432))
                self.assertEqual(dialog.windowModality(), Qt.WindowModal)
                self.assertEqual(dialog.windowTitle(), "联系授权方")
                self.assertEqual(dialog._consumer.name, theme_name)
                self.assertEqual(dialog.findChild(QPushButton, "licenseContactClose").text(), "关闭")
                self.assertEqual(
                    dialog.findChild(QLabel, "licenseContactDescription").text(),
                    "微信扫码联系，添加时备注「Lulu 授权」。",
                )
                self.assertIsNone(dialog.findChild(QLabel, "licenseContactPrivacyText"))
                self.assertIsNone(dialog.findChild(QLabel, "licenseContactShortcut"))
                self.assertTrue(dialog.close_button.hasFocus())
                self.assertEqual(dialog.qr_image.focusPolicy(), Qt.NoFocus)
                self.assertEqual(dialog.qr_fallback.focusPolicy(), Qt.NoFocus)
                self.assertFalse(dialog.grab().isNull())
                dialog.close()
        T.apply_theme("warm")

    def test_dialog_copy_wraps_and_paper_card_stays_inside_fixed_window(self):
        dialog = LicenseContactDialog()
        dialog.show()
        self.app.processEvents()

        description = dialog.findChild(QLabel, "licenseContactDescription")
        self.assertTrue(description.wordWrap())
        self.assertGreaterEqual(description.height(), description.sizeHint().height())
        card_rect = dialog.card.geometry()
        self.assertGreaterEqual(card_rect.left(), 0)
        self.assertGreaterEqual(card_rect.top(), 0)
        self.assertLessEqual(card_rect.right(), dialog.width())
        self.assertLessEqual(card_rect.bottom(), dialog.height())
        self.assertGreater(dialog.card.property("paperTextureStrength"), 0)
        dialog.close()

    def test_parent_sizes_center_dialog_and_modal_keeps_authorization_state(self):
        for size in ((760, 520), (820, 560)):
            with self.subTest(size=size):
                service = _StubService()
                gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
                gate.resize(*size)
                gate.code.setText("LULU-UI-LOCAL")
                original_message = gate.message.text()
                original_tone = gate.status_box.property("tone")
                gate.show()
                self.app.processEvents()

                dialog = LicenseContactDialog(gate)
                dialog.show()
                self.app.processEvents()
                self.assertLessEqual(abs(dialog.frameGeometry().center().x() - gate.frameGeometry().center().x()), 1)
                self.assertLessEqual(abs(dialog.frameGeometry().center().y() - gate.frameGeometry().center().y()), 1)
                self.assertEqual(gate.code.text(), "LULU-UI-LOCAL")
                self.assertEqual(gate.message.text(), original_message)
                self.assertEqual(gate.status_box.property("tone"), original_tone)
                self.assertEqual(service.activate_calls, [])
                dialog.close()
                gate.close()

    def test_gate_contact_is_single_instance_and_returns_focus_without_mutation(self):
        service = _StubService()
        gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
        gate.code.setText("LULU-UI-LOCAL")
        original_message = gate.message.text()
        original_tone = gate.status_box.property("tone")
        gate.show()
        self.app.processEvents()
        seen = []

        def reenter_then_close():
            seen.append(id(gate._contact_dialog))
            gate._show_contact_dialog()
            seen.append(id(gate._contact_dialog))
            gate._contact_dialog.reject()

        QTimer.singleShot(0, reenter_then_close)
        gate.contact_btn.click()
        self.app.processEvents()

        self.assertEqual(len(set(seen)), 1)
        self.assertIsNone(gate._contact_dialog)
        self.assertIs(gate.focusWidget(), gate.contact_btn)
        self.assertEqual(gate.code.text(), "LULU-UI-LOCAL")
        self.assertEqual(gate.message.text(), original_message)
        self.assertEqual(gate.status_box.property("tone"), original_tone)
        self.assertEqual(service.activate_calls, [])
        gate.close()

    def test_button_enter_space_escape_and_system_close_all_close_dialog(self):
        def assert_closed(action):
            dialog = LicenseContactDialog()
            dialog.show()
            self.app.processEvents()
            action(dialog)
            self.app.processEvents()
            self.assertFalse(dialog.isVisible())

        assert_closed(lambda dialog: dialog.close_button.click())
        assert_closed(lambda dialog: QTest.keyClick(dialog.close_button, Qt.Key_Return))
        assert_closed(lambda dialog: QTest.keyClick(dialog.close_button, Qt.Key_Space))
        assert_closed(lambda dialog: QTest.keyClick(dialog, Qt.Key_Escape))
        assert_closed(lambda dialog: dialog.close())

    def test_window_modal_is_active_without_triggering_parent_activation(self):
        service = _StubService()
        gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
        gate.show()
        dialog = LicenseContactDialog(gate)
        modal_state = []

        QTimer.singleShot(0, lambda: modal_state.append(QApplication.activeModalWidget() is dialog))
        QTimer.singleShot(20, dialog.reject)
        dialog.exec()
        self.assertEqual(modal_state, [True])
        self.assertEqual(service.activate_calls, [])
        gate.close()

    def test_all_resource_failures_keep_fixed_fallback_without_details(self):
        class NullPixmap:
            def __init__(self):
                pass

            def loadFromData(self, _data, _format):
                return True

            def isNull(self):
                return True

        class ZeroPixmap:
            def __init__(self):
                pass

            def loadFromData(self, _data, _format):
                return True

            def isNull(self):
                return False

            def width(self):
                return 0

            def height(self):
                return 0

        cases = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.png"
            broken = root / "broken.png"
            broken.write_bytes(b"not-a-png")
            cases.extend(
                (
                    ("missing", missing, CONTACT_QR_SHA256, None),
                    ("undecodable", broken, hashlib.sha256(broken.read_bytes()).hexdigest(), None),
                    ("sha_mismatch", self.asset, "0" * 64, None),
                    ("null_pixmap", self.asset, CONTACT_QR_SHA256, NullPixmap),
                    ("zero_size", self.asset, CONTACT_QR_SHA256, ZeroPixmap),
                )
            )
            for name, path, expected_sha, fake_pixmap in cases:
                with self.subTest(case=name):
                    context = patch("src.ui.license_contact.QPixmap", fake_pixmap) if fake_pixmap else nullcontext()
                    with context:
                        dialog = LicenseContactDialog(asset_path=str(path), expected_sha256=expected_sha)
                    self.assertFalse(dialog.qr_available)
                    self.assertEqual((dialog.width(), dialog.height()), (388, 432))
                    self.assertTrue(dialog.qr_well.property("fallback"))
                    self.assertEqual(dialog.qr_fallback.text(), "联系二维码暂不可用，请稍后再试。")
                    for leaked in (str(path), expected_sha, "Traceback", "http"):
                        self.assertNotIn(leaked, dialog.qr_fallback.text())
                    dialog.close()

    def test_verified_bytes_are_the_only_decode_source_after_path_replace_or_delete(self):
        original_read_bytes = Path.read_bytes
        verified_bytes = original_read_bytes(self.asset)
        verified_sha = hashlib.sha256(verified_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacement = root / "replacement.png"
            replacement_image = QImage(280, 262, QImage.Format_RGBA8888)
            replacement_image.fill(QColor("#ff00ff"))
            self.assertTrue(replacement_image.save(str(replacement), "PNG"))

            for mutation in ("replace", "delete"):
                with self.subTest(mutation=mutation):
                    target = root / f"contact-{mutation}.png"
                    target.write_bytes(verified_bytes)
                    read_count = 0

                    def read_then_mutate(path):
                        nonlocal read_count
                        data = original_read_bytes(path)
                        if path == target:
                            read_count += 1
                            if mutation == "replace":
                                target.write_bytes(replacement.read_bytes())
                            else:
                                target.unlink()
                        return data

                    with patch.object(Path, "read_bytes", read_then_mutate):
                        pixmap = _load_qr_pixmap(
                            str(target),
                            expected_sha256=verified_sha,
                            target_dpr=1.0,
                        )

                    self.assertEqual(read_count, 1)
                    self.assertIsNotNone(pixmap)
                    self.assertNotEqual(pixmap.toImage().pixelColor(0, 0), QColor("#ff00ff"))

            unverified = root / "unverified.png"
            unverified.write_bytes(replacement.read_bytes())
            pixmap = _load_qr_pixmap(
                str(unverified),
                expected_sha256=verified_sha,
                target_dpr=1.0,
            )
            self.assertIsNone(pixmap)

    def test_contact_remains_one_of_exactly_four_low_privilege_actions_and_no_network_stack(self):
        service = _StubService()
        gate = LicenseGateWindow(service, on_unlocked=lambda _status: None)
        labels = {button.text().strip() for button in gate.findChildren(QPushButton)}
        self.assertEqual(labels, {"激活 Lulu", "联系授权方", "打开本地数据文件夹", "退出"})
        source = (Path(__file__).resolve().parents[1] / "src" / "ui" / "license_contact.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("requests", "urlopen", "QNetwork", "clipboard", "pyzbar", "cv2", "decode("):
            self.assertNotIn(forbidden, source)
        self.assertIn("FastTransformation", source)
        self.assertIn("KeepAspectRatio", source)
        self.assertIn("background: #FFFFFF", source)
        self.assertIn("T.consumer_theme(T.ACTIVE.name)", source)
        self.assertIn("PaperTextureFrame", source)
        gate.close()


class nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


if __name__ == "__main__":
    unittest.main()
