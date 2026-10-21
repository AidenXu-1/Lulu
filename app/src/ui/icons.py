"""线性图标：把设计稿里的 SVG 渲染成指定颜色/尺寸的 QPixmap。

用法：icon_label("mic", TERRA, 18) 得到一个带图标的 QLabel；或 pixmap(...) 拿 QPixmap。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel


# 内容提取样板的图标语义。只在显式调用 consumer_* helper 时生效，
# 不改变既有页面传入的任意尺寸与描边。
CONSUMER_ICON_SIZES = {"feature": 20, "control": 16, "compact": 13}
CONSUMER_ICON_STROKE = 1.8

# 每个图标是 viewBox=0 0 24 24 的描边路径（stroke=currentColor 占位，渲染时替换）
_PATHS = {
    "browser": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M2 9h20M6 6.5h.1M9 6.5h.1"/>',
    "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    "workspace_grid": '<path d="M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z"/>',
    "workspace_doc": '<path d="M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6"/>',
    "trash": '<path d="M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7"/>',
    "mic": '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "dashboard": '<path d="M3 3v18h18"/><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "summary": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "list": '<line x1="17" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="17" y1="18" x2="3" y2="18"/>',
    "layers": '<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
    "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    "eye_off": '<path d="M17.94 17.94A10.94 10.94 0 0 1 12 19C5.5 19 2 12 2 12a20.3 20.3 0 0 1 5.06-5.94"/><path d="M9.9 4.24A10.85 10.85 0 0 1 12 5c6.5 0 10 7 10 7a20.16 20.16 0 0 1-3.22 4.31"/><path d="M14.12 14.12A3 3 0 0 1 9.88 9.88"/><line x1="2" y1="2" x2="22" y2="22"/>',
    "export": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "hdd": '<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/>',
    "play": '<polygon points="6 4 20 12 6 20 6 4"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "cloud": '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "pen": '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    "chevron": '<polyline points="9 18 15 12 9 6"/>',
    "chevron_down": '<polyline points="6 9 12 15 18 9"/>',
    "video": '<rect x="3" y="5" width="12" height="14" rx="2"/><path d="m15 10 6-4v12l-6-4z"/>',
    "caret_down": '<polyline points="6 9 12 15 18 9"/>',
    # 面板折叠图标（IDE 风格：边框 + 一条竖线分出侧栏）
    "panel_left": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>',
    "panel_right": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/>',
    # 头戴式耳机（内容提取）
    "headphones": '<path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>',
    # 单人（主页采集）
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    # 音频波形（音频库）
    "waveform": '<line x1="4" y1="9" x2="4" y2="15"/><line x1="8" y1="5" x2="8" y2="19"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="16" y1="3" x2="16" y2="21"/><line x1="20" y1="9" x2="20" y2="15"/>',
    # 大脑（AI 大模型）：标准 Lucide brain
    "brain": '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/>',
    # 设置中心存储语义：同一 24px 网格、圆端点、2px 描边，不复用业务导航图标。
    "storage_document": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="14" y2="17"/>',
    "storage_media": '<path d="M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/><polyline points="9 9 12 12 15 9"/><line x1="12" y1="12" x2="12" y2="16"/>',
    "storage_recording": '<rect x="6" y="2" width="12" height="15" rx="6"/><path d="M4 12a8 8 0 0 0 16 0"/><line x1="12" y1="20" x2="12" y2="22"/>',
    "storage_voice_output": '<path d="M4 9v6"/><path d="M8 5v14"/><path d="M12 8v8"/><path d="M16 3v18"/><path d="M20 9v6"/><polyline points="15 7 18 4 21 7"/>',
    "storage_voice_library": '<path d="M4 3h14a2 2 0 0 1 2 2v16H6a2 2 0 0 1-2-2z"/><path d="M4 17h16"/><path d="M8 8v4"/><path d="M11 6v8"/><path d="M14 9v2"/>',
    "storage_ai_models": '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 9h6v6H9z"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
}


# 用 PNG 美术稿当图标（单色线稿+透明底），按其 alpha 上色，和线性图标一样能随选中态变色。
_ASSET_ICONS = {
    "ai_cube": "ai_cube.png",   # AI 大模型：立方体 + Ai
}

# 多色品牌 logo：原色显示、不随颜色变（pixmap 的 color 参数对它们无效）。
_BRAND_ICONS = {
    "feishu": "feishu.png",
    "obsidian": "obsidian.png",
}


def _asset_path(filename: str) -> str:
    """开发/打包两种环境下定位 ui/assets 资源。"""
    here = Path(__file__).resolve().parent / "assets" / filename
    if here.exists():
        return str(here)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for c in (Path(base) / "src" / "ui" / "assets" / filename, Path(base) / "assets" / filename):
            if c.exists():
                return str(c)
    return str(here)


@lru_cache(maxsize=128)
def _tinted_image(path: str, color: str, size: int) -> QPixmap:
    """把单色 PNG 线稿按目标颜色重新着色（保留 alpha 形状）。高清 2x。"""
    dpr = 2
    src = QPixmap(path)
    if src.isNull():
        return QPixmap()
    src = src.scaled(size * dpr, size * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QPixmap(src.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)  # 用形状的 alpha 当蒙版，填目标色
    p.fillRect(out.rect(), QColor(color))
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


@lru_cache(maxsize=128)
def _brand_image(path: str, size: int) -> QPixmap:
    """多色品牌 logo：裁掉四周留白、等比缩放、2x，不上色。"""
    img = QImage(path)
    if img.isNull():
        return QPixmap()
    img = img.convertToFormat(QImage.Format_ARGB32)
    box = _alpha_bbox(img)
    if box is not None:
        img = img.copy(box)
    dpr = 2
    pm = QPixmap.fromImage(img).scaled(
        size * dpr, size * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int = 18, stroke: float = 2.0) -> QPixmap:
    """渲染线性图标为 QPixmap。按 (name,color,size,stroke) 缓存，避免反复重绘 SVG。"""
    if name in _BRAND_ICONS:
        return _brand_image(_asset_path(_BRAND_ICONS[name]), size)
    if name in _ASSET_ICONS:
        return _tinted_image(_asset_path(_ASSET_ICONS[name]), color, size)
    inner = _PATHS.get(name, "")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{inner}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    dpr = 2  # 高清屏
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(p, QRectF(0, 0, size, size))  # 显式按逻辑尺寸渲染，避免裁切
    p.end()
    return pm


@lru_cache(maxsize=64)
def icon_file(name: str, color: str, size: int = 12, stroke: float = 2.0) -> str:
    """把图标渲染成 PNG 落到临时目录，返回可在 QSS `image:url(...)` 里引用的路径。"""
    import os
    import tempfile

    pm = pixmap(name, color, size, stroke)
    safe = color.lstrip("#")
    path = os.path.join(tempfile.gettempdir(), f"lulu_icon_{name}_{safe}_{size}.png")
    pm.save(path, "PNG")
    return path


def rounded_logo(path: str, size: int, radius: int = 11) -> QPixmap:
    """把（无透明的）方形 logo 居中裁切并加圆角，得到尺寸为 size 的高清圆角徽标。"""
    dpr = 2
    s = size * dpr
    src = QPixmap(path)
    if src.isNull():
        return QPixmap()
    src = src.scaled(s, s, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    if src.width() != s or src.height() != s:
        x = max(0, (src.width() - s) // 2)
        y = max(0, (src.height() - s) // 2)
        src = src.copy(x, y, s, s)
    out = QPixmap(s, s)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    path_ = QPainterPath()
    path_.addRoundedRect(QRectF(0, 0, s, s), radius * dpr, radius * dpr)
    p.setClipPath(path_)
    p.drawPixmap(0, 0, src)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def icon_label(name: str, color: str, size: int = 18, stroke: float = 2.0) -> QLabel:
    lab = QLabel()
    lab.setPixmap(pixmap(name, color, size, stroke))
    lab.setFixedSize(size, size)
    lab.setAlignment(Qt.AlignCenter)
    return lab


def consumer_pixmap(name: str, color: str, role: str = "control") -> QPixmap:
    """按样板语义渲染线性图标，避免同级控件出现随意尺寸和线重。"""
    if role not in CONSUMER_ICON_SIZES:
        raise ValueError(f"unsupported consumer icon role: {role}")
    return pixmap(name, color, CONSUMER_ICON_SIZES[role], CONSUMER_ICON_STROKE)


def consumer_icon_label(name: str, color: str, role: str = "control") -> QLabel:
    """返回已按 feature/control/compact 三档规范化的样板图标。"""
    if role not in CONSUMER_ICON_SIZES:
        raise ValueError(f"unsupported consumer icon role: {role}")
    return icon_label(name, color, CONSUMER_ICON_SIZES[role], CONSUMER_ICON_STROKE)


@lru_cache(maxsize=64)
def image_pixmap(path: str, size: int = 18) -> QPixmap:
    """加载原图（如品牌 logo）并按 size 等比缩放，保留透明、不上色。高清 2x。"""
    dpr = 2
    src = QPixmap(path)
    if src.isNull():
        return QPixmap()
    scaled = src.scaled(size * dpr, size * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    return scaled


def image_label(path: str, size: int = 18) -> QLabel:
    """品牌 logo 用：原色显示（不随选中态变色），在固定方框内居中。"""
    lab = QLabel()
    lab.setPixmap(image_pixmap(path, size))
    lab.setFixedSize(size, size)
    lab.setAlignment(Qt.AlignCenter)
    return lab


# 互动数据图标：兆基提供的彩色 PNG；白色线稿（评论/转发）按 alpha 上色成可见色。
_STAT_ICON = {
    "like": ("stat_like.png", None),            # 红心
    "favorite": ("stat_favorite.png", None),    # 金星
    "comment": ("stat_comment.png", "#8A93A0"), # 白→中性蓝灰
    "share": ("stat_share.png", "#5B8C7B"),     # 白→青绿
    "recommend": ("stat_recommend.png", None),  # 绿色拇指
    "coin": ("stat_coin.png", None),            # 粉色投币
    "danmaku": ("stat_danmaku.png", "#FD659D"), # 白→与投币同款粉
}


def _alpha_bbox(img: QImage) -> "QRectF | None":
    """非透明像素的外接框（采样步进求，用于裁掉图标四周留白）。"""
    # Read alpha bytes in bulk instead of constructing a QColor per pixel.
    # Preserve the existing threshold and sampling grid, including padded rows.
    alpha = img.convertToFormat(QImage.Format_Alpha8)
    w, h, stride = alpha.width(), alpha.height(), alpha.bytesPerLine()
    step = max(1, min(w, h) // 200)
    data = bytes(alpha.constBits())
    threshold = bytes(0 if value <= 16 else 1 for value in range(256))
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(0, h, step):
        row = data[y*stride:y*stride+w:step].translate(threshold)
        first = row.find(b'\x01')
        if first >= 0:
            x0, y0 = min(x0, first*step), min(y0, y)
            x1, y1 = max(x1, row.rfind(b'\x01')*step), y
    if x1 < 0:
        return None
    from PySide6.QtCore import QRect
    return QRect(x0, y0, max(1, x1 - x0 + step), max(1, y1 - y0 + step))


@lru_cache(maxsize=128)
def stat_pixmap(key: str, size: int = 14):
    """互动指标图标 → QPixmap（裁掉留白、按需上色、高清 2x）。无此 key 返回 None。"""
    spec = _STAT_ICON.get(key)
    if not spec:
        return None
    fname, tint = spec
    img = QImage(_asset_path(fname))
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format_ARGB32)
    box = _alpha_bbox(img)
    if box is not None:
        img = img.copy(box)
    dpr = 2
    pm = QPixmap.fromImage(img).scaled(
        size * dpr, size * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if tint:
        out = QPixmap(pm.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.drawPixmap(0, 0, pm)
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(out.rect(), QColor(tint))
        p.end()
        pm = out
    pm.setDevicePixelRatio(dpr)
    return pm
