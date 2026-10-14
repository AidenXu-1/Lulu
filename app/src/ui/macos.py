"""macOS 原生窗口微调：透明标题栏 + 内容延伸到顶，让顶部与内容连通（无独立灰标题栏）。

纯 ctypes 调 Objective-C 运行时，不引入新依赖；仅 macOS 生效，任何失败都静默回退到
普通标题栏。与 QSS/主题完全无关（这是窗口 chrome，不是样式表），不影响多主题。
"""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import Qt

# AppKit 常量
_NS_FULL_SIZE_CONTENT_VIEW = 1 << 15   # NSWindowStyleMaskFullSizeContentView
_NS_TITLE_HIDDEN = 1                    # NSWindowTitleHidden
_NS_TITLEBAR_SEPARATOR_AUTOMATIC = 0
_NS_TITLEBAR_SEPARATOR_NONE = 1

# 顶部为红绿灯 / 拖拽留出的空白高度（逻辑像素）
TITLEBAR_INSET = 26
_NS_APPLICATION_ACTIVATION_POLICY_REGULAR = 0


class _NSEdgeInsets(ctypes.Structure):
    _fields_ = (
        ("top", ctypes.c_double),
        ("left", ctypes.c_double),
        ("bottom", ctypes.c_double),
        ("right", ctypes.c_double),
    )


def activate_regular_app() -> bool:
    """源码态 python 在 macOS 下显式注册成普通前台 App。

    从终端 / Codex 直接跑 `python -m src.main` 时,NSApplication 偶发保持在
    非前台策略,进程存活且 Qt 事件循环在跑,但 System Events 看不到窗口。
    这里不引入 PyObjC,只用 Objective-C runtime 做 best-effort 修正。
    """
    if sys.platform != "darwin":
        return False
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p

        def sel(name: str):
            return objc.sel_registerName(name.encode())

        def send(recv, selector, *args, restype=ctypes.c_void_p, argtypes=()):
            objc.objc_msgSend.restype = restype
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(recv, selector, *args)

        nsapp_cls = objc.objc_getClass(b"NSApplication")
        nsapp = send(nsapp_cls, sel("sharedApplication"))
        if not nsapp:
            return False
        send(
            nsapp,
            sel("setActivationPolicy:"),
            _NS_APPLICATION_ACTIVATION_POLICY_REGULAR,
            restype=ctypes.c_bool,
            argtypes=(ctypes.c_long,),
        )
        send(nsapp, sel("activateIgnoringOtherApps:"), True, restype=None, argtypes=(ctypes.c_bool,))
        return True
    except Exception:  # noqa: BLE001 — 原生调用失败就交给 Qt 默认策略
        return False


def unify_titlebar(window) -> bool:
    """把窗口标题栏改为透明、隐藏标题、内容延伸到顶。成功返回 True。"""
    if sys.platform != "darwin":
        return False
    try:
        # Keep Qt's own window state consistent with AppKit. A Cocoa-only style
        # mask is reset by Qt when showing or restoring the window again.
        flags = window.windowFlags() | Qt.ExpandedClientAreaHint | Qt.NoTitleBarBackgroundHint
        if flags != window.windowFlags():
            window.setWindowFlags(flags)
        # MainWindow reserves its own 43 px titlebar band; do not add Qt's
        # automatic safe-area inset a second time around that layout.
        window.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p

        def sel(name: str):
            return objc.sel_registerName(name.encode())

        def send(recv, selector, *args, restype=ctypes.c_void_p, argtypes=()):
            objc.objc_msgSend.restype = restype
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(recv, selector, *args)

        nsview = ctypes.c_void_p(int(window.winId()))   # winId() 即 NSView*
        nswindow = send(nsview, sel("window"))
        if not nswindow:
            return False
        send(nswindow, sel("setTitlebarAppearsTransparent:"), True,
             restype=None, argtypes=(ctypes.c_bool,))
        cur = send(nswindow, sel("styleMask"), restype=ctypes.c_ulong)
        send(nswindow, sel("setStyleMask:"), cur | _NS_FULL_SIZE_CONTENT_VIEW,
             restype=None, argtypes=(ctypes.c_ulong,))
        send(nswindow, sel("setTitleVisibility:"), _NS_TITLE_HIDDEN,
             restype=None, argtypes=(ctypes.c_long,))
        # 不开 movableByWindowBackground：它会把顶栏按钮的点击当成拖窗吞掉
        # （顶部透明标题栏那条窄区仍可原生拖动窗口）
        return True
    except Exception:  # noqa: BLE001 — 原生调用失败就回退普通标题栏
        return False


def set_page_titlebar_immersive(window, enabled: bool) -> bool:
    """Toggle the sample page's separator without changing other pages' chrome.

    ``unify_titlebar`` establishes Lulu's existing transparent/full-size window.
    This page-level switch only removes the native separator while the
    transcription sample is active, then restores the exact prior separator
    style when the user leaves that page.
    """
    if sys.platform != "darwin":
        return False
    application = QGuiApplication.instance()
    if application is None:
        return False
    try:
        qt_platform = str(application.platformName()).strip().lower()
    except Exception:  # noqa: BLE001 — 无法确认真实 Qt 平台时必须失败关闭
        return False
    if qt_platform != "cocoa":
        return False
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p

        def sel(name: str):
            return objc.sel_registerName(name.encode())

        def send(recv, selector, *args, restype=ctypes.c_void_p, argtypes=()):
            objc.objc_msgSend.restype = restype
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(recv, selector, *args)

        nsview = ctypes.c_void_p(int(window.winId()))
        nswindow = send(nsview, sel("window"))
        if not nswindow:
            return False
        getter = sel("titlebarSeparatorStyle")
        setter = sel("setTitlebarSeparatorStyle:")
        if not send(
            nswindow,
            sel("respondsToSelector:"),
            getter,
            restype=ctypes.c_bool,
            argtypes=(ctypes.c_void_p,),
        ) or not send(
            nswindow,
            sel("respondsToSelector:"),
            setter,
            restype=ctypes.c_bool,
            argtypes=(ctypes.c_void_p,),
        ):
            return False

        saved = getattr(window, "_lulu_titlebar_separator_style", None)
        if enabled:
            if saved is None:
                saved = int(send(nswindow, getter, restype=ctypes.c_long))
                window._lulu_titlebar_separator_style = saved
            send(
                nswindow,
                sel("setTitlebarAppearsTransparent:"),
                True,
                restype=None,
                argtypes=(ctypes.c_bool,),
            )
            cur = send(nswindow, sel("styleMask"), restype=ctypes.c_ulong)
            send(
                nswindow,
                sel("setStyleMask:"),
                cur | _NS_FULL_SIZE_CONTENT_VIEW,
                restype=None,
                argtypes=(ctypes.c_ulong,),
            )
            target = _NS_TITLEBAR_SEPARATOR_NONE
        else:
            target = (
                int(saved)
                if saved is not None
                else _NS_TITLEBAR_SEPARATOR_AUTOMATIC
            )
        send(
            nswindow,
            setter,
            target,
            restype=None,
            argtypes=(ctypes.c_long,),
        )
        send(nswindow, sel("setTitleVisibility:"), _NS_TITLE_HIDDEN,
             restype=None, argtypes=(ctypes.c_long,))
        return True
    except Exception:  # noqa: BLE001 — 原生能力不可用时由调用方保持既有外壳
        return False


def native_safe_area_top(window) -> int | None:
    """Return Cocoa's actual content-view safe top, or None on any uncertainty."""
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_msgSend.restype = ctypes.c_void_p

        def sel(name: str):
            return objc.sel_registerName(name.encode())

        def send(recv, selector, *args, restype=ctypes.c_void_p, argtypes=()):
            objc.objc_msgSend.restype = restype
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(recv, selector, *args)

        nsview = ctypes.c_void_p(int(window.winId()))
        nswindow = send(nsview, sel("window"))
        if not nswindow:
            return None
        content = send(nswindow, sel("contentView"))
        if not content:
            return None
        safe = send(content, sel("safeAreaInsets"), restype=_NSEdgeInsets)
        value = round(float(safe.top))
        return value if value >= 0 else None
    except Exception:  # noqa: BLE001 — 原生几何不可信时必须回退旧布局
        return None
