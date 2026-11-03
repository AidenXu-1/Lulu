"""Isolated navigation surface: no Core, settings, model or user documents."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QRectF,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame
from src.ui import theme
from src.ui.workbench_navigation import WorkbenchNavigation
from src.ui.control_foundation import adopt_nav_row


class WorkbenchNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_rail(self, reduced=False):
        with patch('src.ui.workbench_navigation.reduced_motion', return_value=reduced):
            rail = WorkbenchNavigation(theme.consumer_theme('warm'))
        for index in (0, 5, 4, 9):
            row = QFrame(); row.setFixedHeight(40)
            adopt_nav_row(row,on_activate=lambda i=index:rail.select(i))
            rail.add_row(index, row)
        rail.resize(200, 172); rail.show(); self.app.processEvents()
        self.addCleanup(rail.close)
        self.addCleanup(rail.deleteLater)
        return rail

    def test_rapid_switch_continues_from_visible_position_and_latest_destination_wins(self):
        rail = self.make_rail()
        rail.select(0); rail.select(9)
        rail.animation.setCurrentTime(70)
        middle = QRectF(rail.indicator)
        rail.select(5)
        self.assertEqual(rail.animation.startValue(), middle)
        rail.animation.setCurrentTime(240)
        self.assertEqual(rail.indicator, QRectF(rail.rows[5].geometry()).adjusted(.5,.5,-.5,-.5))
        self.assertEqual(rail.current_index, 5)
        rail.select(2)
        self.assertTrue(rail.indicator.isEmpty())

    def test_reduced_motion_and_keyboard_are_immediate_and_resize_keeps_alignment(self):
        rail = self.make_rail(reduced=True)
        rail.select(0); rail.select(9)
        self.assertEqual(rail.animation.state(), rail.animation.State.Stopped)
        rail.motion_enabled = True
        QTest.keyClick(rail.rows[4],Qt.Key.Key_Return)
        self.assertEqual(rail.animation.state(), rail.animation.State.Stopped)
        self.assertEqual(rail.current_index,4)
        QTest.mouseClick(rail.rows[0],Qt.MouseButton.LeftButton)
        self.assertTrue(rail.animation_allowed)
        self.assertEqual(rail.current_index,0)
        rail.select(4)
        rail.resize(160, 172); self.app.processEvents()
        self.assertEqual(rail.indicator, QRectF(rail.rows[4].geometry()).adjusted(.5,.5,-.5,-.5))
