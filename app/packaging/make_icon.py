"""生成 App 图标:用已确认的完整猫咪品牌主图生成圆角方块。

产出 1024×1024 PNG → 交给 sips/iconutil 合成 .icns(见 build_app.sh)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QPainter, QPainterPath, QPixmap

S = 1024
RADIUS = 230  # ≈ 22.5%,macOS 圆角方块观感
SUBJECT_SCALE = 1.08
APP_ICON_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "assets"
    / "brand"
    / "lulu-logo-mascot-transparent-20260718.png"
)


def main(out_png: str) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)  # noqa: F841

    src = QPixmap(str(APP_ICON_SOURCE))
    if src.isNull():
        raise SystemExit(f"找不到 App Icon 品牌主图:{APP_ICON_SOURCE}")
    subject_size = round(S * SUBJECT_SCALE)
    src = src.scaled(
        subject_size,
        subject_size,
        Qt.KeepAspectRatioByExpanding,
        Qt.SmoothTransformation,
    )
    if src.width() != S or src.height() != S:
        x = max(0, (src.width() - S) // 2)
        y = max(0, (src.height() - S) // 2)
        src = src.copy(x, y, S, S)

    out = QPixmap(S, S)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, S, S), RADIUS, RADIUS)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src)
    p.end()
    out.save(out_png, "PNG")
    print("icon PNG ->", out_png)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "icon_1024.png")
