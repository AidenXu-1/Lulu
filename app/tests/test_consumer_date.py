"""Focused tests for Lulu's themed date field and calendar popup."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QRect, Qt  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCalendarWidget  # noqa: E402

from src.ui import theme as T  # noqa: E402
from src.ui.consumer_date import ConsumerDate  # noqa: E402
from src.ui.control_foundation import PaperTextureFrame  # noqa: E402


class ConsumerDateTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        T.apply_theme("warm")

    def tearDown(self):
        T.apply_theme("warm")

    def _date(self, value=QDate(2026, 7, 26)):
        field = ConsumerDate(value, accessible_name="开始日期")
        field.resize(138, 38)
        field.move(80, 80)
        field.show()
        self.app.processEvents()
        return field

    def test_field_has_lulu_arrow_and_never_uses_native_calendar_widget(self):
        field = self._date()
        self.assertFalse(field.trigger.icon().isNull())
        self.assertEqual(field.editor.text(), "2026-07-26")
        self.assertEqual(field.editor.accessibleName(), "开始日期")
        self.assertEqual(field.objectName(), "consumerDateField")
        self.assertIn("QWidget#consumerDateField{", field.styleSheet())
        self.assertIn(
            "QLineEdit#consumerDateEditor{background:transparent",
            field.styleSheet(),
        )
        self.assertIn(
            "border:none;border-left:1px solid",
            field.styleSheet(),
        )
        self.assertEqual(field.findChildren(QCalendarWidget), [])
        self.assertIsInstance(field.popup.paper, PaperTextureFrame)
        self.assertEqual(len(field.popup.day_buttons), 42)

    def test_editor_and_arrow_share_one_focus_frame(self):
        field = self._date()
        self.assertFalse(field.property("focused"))
        field.editor.setFocus()
        self.app.processEvents()
        self.assertTrue(field.property("focused"))
        field.editor.clearFocus()
        self.app.processEvents()
        self.assertFalse(field.property("focused"))

    def test_paper_cover_calendar_semantics_and_real_rounding(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                T.apply_theme(theme_name)
                field = self._date()
                field.refresh_theme()
                consumer = T.consumer_theme(theme_name)
                qss = field.popup.styleSheet()
                for token in (
                    consumer.surface_floating,
                    consumer.calendar_weekend,
                    consumer.calendar_disabled,
                    consumer.accent,
                    consumer.secondary_hover,
                ):
                    self.assertIn(token, qss)
                field.show_popup()
                self.app.processEvents()
                self.assertTrue(field.popup.isVisible())
                self.assertFalse(field.popup.paper.mask().isEmpty())
                image = field.popup.grab().toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                self.assertGreater(
                    image.pixelColor(image.width() // 2, image.height() // 2).alpha(),
                    0,
                )
                field.popup.close()

    def test_valid_edit_emits_and_invalid_edit_restores_last_valid_date(self):
        field = self._date()
        changed = QSignalSpy(field.dateChanged)
        field.editor.setText("2026-08-03")
        field._commit_editor()
        self.assertEqual(field.date(), QDate(2026, 8, 3))
        self.assertEqual(changed.count(), 1)
        field.editor.setText("2026-99-99")
        field._commit_editor()
        self.assertEqual(field.date(), QDate(2026, 8, 3))
        self.assertEqual(field.editor.text(), "2026-08-03")
        self.assertEqual(changed.count(), 1)

    def test_popup_is_anchored_below_and_keyboard_selects_without_native_chrome(self):
        field = self._date()
        field.show_popup()
        self.app.processEvents()
        trigger_bottom = field.rect().bottomLeft()
        trigger_bottom = field.mapToGlobal(trigger_bottom).y()
        self.assertGreaterEqual(field.popup.y() - trigger_bottom, 6)
        self.assertLessEqual(field.popup.y() - trigger_bottom, 8)
        QTest.keyClick(field.popup, Qt.Key.Key_Right)
        QTest.keyClick(field.popup, Qt.Key.Key_Return)
        self.assertEqual(field.date(), QDate(2026, 7, 27))
        self.assertFalse(field.popup.isVisible())

    def test_insufficient_lower_space_fails_closed_without_visible_label(self):
        field = self._date()
        fake_screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 320, 40))
        with patch(
            "src.ui.consumer_date.QGuiApplication.screenAt",
            return_value=fake_screen,
        ):
            field.show_popup()
        self.app.processEvents()
        self.assertFalse(field.popup.isVisible())
        self.assertEqual(field.editor.toolTip(), "窗口下方空间不足")
        self.assertEqual(field.editor.accessibleDescription(), "窗口下方空间不足")


if __name__ == "__main__":
    unittest.main()
