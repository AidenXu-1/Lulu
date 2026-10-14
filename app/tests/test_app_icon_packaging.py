from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QImage


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent
EXPECTED_SOURCE = (
    REPO_DIR
    / "docs"
    / "assets"
    / "brand"
    / "lulu-logo-mascot-transparent-20260718.png"
)
EXPECTED_GENERATED_SHA256 = (
    "4570a75d237cca043dbf29c44d365d9477703b044ce425f029c1d88655b80076"
)


class AppIconPackagingTests(unittest.TestCase):
    def test_confirmed_mascot_asset_is_the_packaging_source(self) -> None:
        source = (APP_DIR / "packaging" / "make_icon.py").read_text(encoding="utf-8")
        self.assertIn("lulu-logo-mascot-transparent-20260718.png", source)
        self.assertIn("SUBJECT_SCALE = 1.08", source)
        self.assertNotIn('"logo.png"', source)
        self.assertEqual(
            hashlib.sha256(EXPECTED_SOURCE.read_bytes()).hexdigest(),
            "52fd54032cae66fc3d7704eaddeac889a9d82417a27b25f447787ff5ea144cb6",
        )

    def test_generated_icon_is_square_and_keeps_transparent_corners(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            subprocess.run(
                [sys.executable, "packaging/make_icon.py", str(output)],
                cwd=APP_DIR,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            image = QImage(str(output))
            self.assertFalse(image.isNull())
            self.assertEqual((image.width(), image.height()), (1024, 1024))
            self.assertEqual(
                hashlib.sha256(output.read_bytes()).hexdigest(),
                EXPECTED_GENERATED_SHA256,
            )
            for x, y in ((0, 0), (1023, 0), (0, 1023), (1023, 1023)):
                self.assertEqual(image.pixelColor(x, y).alpha(), 0)
            self.assertGreater(image.pixelColor(512, 512).alpha(), 0)

    def test_generated_icon_remains_visible_at_required_small_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            subprocess.run(
                [sys.executable, "packaging/make_icon.py", str(output)],
                cwd=APP_DIR,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            image = QImage(str(output))
            for size in (16, 32, 64, 128):
                with self.subTest(size=size):
                    thumbnail = image.scaled(
                        size,
                        size,
                    )
                    self.assertFalse(thumbnail.isNull())
                    self.assertGreater(thumbnail.pixelColor(size // 2, size // 2).alpha(), 0)
                    self.assertTrue(
                        any(
                            thumbnail.pixelColor(x, y).alpha() > 0
                            for y in range(size)
                            for x in range(size)
                        )
                    )
                    self.assertTrue(
                        any(
                            (color := thumbnail.pixelColor(x, y)).alpha() > 128
                            and color.blue() > color.red() + 15
                            and color.blue() > color.green() + 5
                            for y in range(size)
                            for x in range(size)
                        ),
                        "耳机蓝色在缩略图中必须仍可辨识",
                    )
                    self.assertTrue(
                        any(
                            (color := thumbnail.pixelColor(x, y)).alpha() > 128
                            and color.red() < 90
                            and color.green() < 90
                            and color.blue() < 90
                            for y in range(size)
                            for x in range(size)
                        ),
                        "眼睛与轮廓深色在缩略图中必须仍可辨识",
                    )



if __name__ == "__main__":
    unittest.main()
