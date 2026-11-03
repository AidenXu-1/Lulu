"""Measure the actual tab painting, without live controllers or recordings."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication
from src.ui import live_workspace_controls as controls


class LiveTabLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_label_and_count_are_centered_together_and_long_counts_fit(self):
        draws = []
        class Painter(QPainter):
            def drawText(self, *args):
                if len(args) == 3:
                    rect, flags, text = args
                    draws.append((QRectF(rect), text))
                return super().drawText(*args)
        with patch.object(controls, 'QPainter', Painter):
            for label in ('主播监控', '录像库'):
                for selected in (False, True):
                    for number in ('1', '2', '100', '10000', '100000000'):
                        with self.subTest(label=label, selected=selected, count=number):
                            tab = controls.WorkspaceTab(label+'  '+number)
                            tab.setChecked(selected)
                            draws.clear(); tab.grab()
                            text_rect, _ = draws[-2]; badge_rect, displayed = draws[-1]
                            left = text_rect.left(); right = tab.width()-badge_rect.right()
                            self.assertAlmostEqual(left, right, delta=1)
                            self.assertGreaterEqual(left, 6)
                            self.assertGreaterEqual(right, 6)
                            self.assertTrue(displayed)
                            self.assertEqual(tab.text(), label+'  '+number)
                            tab.close(); tab.deleteLater()
