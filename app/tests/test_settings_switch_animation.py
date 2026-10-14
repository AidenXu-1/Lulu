import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractAnimation, QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.ui import theme as T
from src.ui.consumer_switch import ConsumerSwitch


class SettingsSwitchAnimationTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        T.apply_theme("warm")

    def tearDown(self):
        T.apply_theme("warm")

    def test_mouse_toggle_has_real_middle_frame_and_fixed_geometry(self):
        switch = ConsumerSwitch(accessible_name="动画开关")
        switch.set_checked(False, animate=False)
        switch.show()
        self.app.processEvents()
        geometry = switch.geometry()

        QTest.mouseClick(switch, Qt.LeftButton)
        QTest.qWait(55)
        self.assertEqual(switch.animation.state(), QAbstractAnimation.Running)
        self.assertGreater(switch.position, 0.0)
        self.assertLess(switch.position, 1.0)
        self.assertEqual(switch.geometry(), geometry)

        QTest.qWait(180)
        self.assertAlmostEqual(switch.position, 1.0, places=3)
        self.assertEqual(switch.geometry(), geometry)
        switch.close()

    def test_programmatic_failure_rollback_animates_back(self):
        switch = ConsumerSwitch(accessible_name="保存失败回滚")
        switch.set_checked(True, animate=False)
        self.assertAlmostEqual(switch.position, 1.0, places=3)

        switch.set_checked(False, animate=True)
        QTest.qWait(55)
        self.assertGreater(switch.position, 0.0)
        self.assertLess(switch.position, 1.0)
        QTest.qWait(180)
        self.assertAlmostEqual(switch.position, 0.0, places=3)
        self.assertFalse(switch.isChecked())

    def test_public_set_checked_keeps_qt_signal_and_animation_contract(self):
        switch = ConsumerSwitch(accessible_name="代码切换")
        switch.set_checked(False, animate=False)
        emitted = []
        switch.toggled.connect(emitted.append)
        switch.show()
        self.app.processEvents()

        switch.setChecked(True)
        QTest.qWait(55)
        self.assertEqual(emitted, [True])
        self.assertGreater(switch.position, 0.0)
        self.assertLess(switch.position, 1.0)
        QTest.qWait(180)
        self.assertAlmostEqual(switch.position, 1.0, places=3)
        switch.close()

    def test_keyboard_accessibility_and_paper_rendering(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                switch = ConsumerSwitch(accessible_name="任务开关")
                switch.set_checked(False, animate=False)
                switch.show()
                switch.setFocus()
                self.app.processEvents()
                baseline = switch.geometry()
                QTest.keyClick(switch, Qt.Key_Space)
                QTest.qWait(190)
                self.assertTrue(switch.isChecked())
                self.assertEqual(switch.accessibleName(), "任务开关")
                self.assertEqual(switch.accessibleDescription(), "已开启")
                self.assertEqual(switch.geometry(), baseline)
                self.assertFalse(switch.grab().isNull())
                switch.close()

    def test_mouse_cancel_returns_to_initial_pixels_and_tab_focus_is_neutral(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                switch = ConsumerSwitch(accessible_name="取消语义")
                switch.set_checked(False, animate=False)
                switch.show()
                switch.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
                self.app.processEvents()
                initial = switch.grab().toImage()
                QTest.mouseClick(switch, Qt.LeftButton)
                QTest.qWait(190)
                QTest.mouseClick(switch, Qt.LeftButton)
                QTest.qWait(190)
                QApplication.sendEvent(switch, QEvent(QEvent.Type.Leave))
                switch.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
                self.app.processEvents()
                self.assertFalse(switch.isChecked())
                self.assertFalse(switch._keyboard_focus)
                self.assertEqual(switch.grab().toImage(), initial)
                QApplication.sendEvent(
                    switch,
                    QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason),
                )
                self.assertTrue(switch._keyboard_focus)
                self.assertFalse(switch.grab().isNull())
                switch.close()


if __name__ == "__main__":
    unittest.main()
