import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QProgressBar

from src.core.progress import map_fraction, percent_text, stage_callback, valid_fraction
from src.ui.progress_feedback import present_progress, reset_progress


class CoreProgressTests(unittest.TestCase):
    def test_fraction_normalisation_rejects_unknown_and_clamps_bounds(self):
        self.assertIsNone(valid_fraction(-1))
        self.assertIsNone(valid_fraction(math.nan))
        self.assertIsNone(valid_fraction(math.inf))
        self.assertIsNone(valid_fraction(True))
        self.assertEqual(valid_fraction(-0.0), 0.0)
        self.assertEqual(valid_fraction(2.5), 1.0)

    def test_stage_callback_preserves_unknown_and_maps_known_fraction(self):
        events: list[tuple[str, float]] = []
        callback = stage_callback(lambda message, value: events.append((message, value)), 0.2, 0.8)

        callback("下载中", 0.5)
        callback("等待上游", -1)

        self.assertAlmostEqual(events[0][1], 0.5)
        self.assertEqual(events[1], ("等待上游", -1.0))
        self.assertAlmostEqual(map_fraction(1.0, 0.2, 0.8), 0.8)
        self.assertEqual(percent_text(0.634, approximate=True), "约 63%")


class ProgressPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_unknown_holds_last_trusted_position_without_busy_sweep(self):
        bar = QProgressBar()
        label = QLabel()
        reset_progress(bar, label)

        present_progress(bar, 0.42, label=label)
        result = present_progress(bar, -1, label=label, unknown_text="正在加载模型")

        self.assertIsNone(result)
        self.assertEqual((bar.minimum(), bar.maximum()), (0, 100))
        self.assertEqual(bar.value(), 42)
        self.assertEqual(label.text(), "正在加载模型")
        self.assertTrue(bar.property("luluProgressUnknown"))
        self.assertIn("42%", bar.accessibleDescription())

    def test_progress_is_monotonic_and_completion_is_not_approximate(self):
        bar = QProgressBar()
        label = QLabel()
        reset_progress(bar, label)

        present_progress(bar, 0.68, label=label, approximate=True)
        present_progress(bar, 0.20, label=label, approximate=True)
        self.assertEqual(bar.value(), 68)
        self.assertEqual(label.text(), "约 68%")

        present_progress(bar, 1.0, label=label, approximate=True)
        self.assertEqual(bar.value(), 100)
        self.assertEqual(label.text(), "100%")


if __name__ == "__main__":
    unittest.main()
