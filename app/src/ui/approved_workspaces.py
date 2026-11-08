"""Package-local rendering for workspaces whose interaction design is frozen."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from shiboken6 import isValid

from .agent_workspace.runtime import prepare_webengine_runtime
from .theme import asset


class LocalResources(QWebEngineUrlRequestInterceptor):
    def __init__(self, files, parent):
        super().__init__(parent)
        self.files = frozenset(files)

    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = url == QUrl('qrc:/qtwebchannel/qwebchannel.js')
        if url.isLocalFile():
            allowed = Path(url.toLocalFile()).resolve() in self.files
        info.block(not allowed)


class LocalPage(QWebEnginePage):
    def __init__(self, profile, entry, parent):
        super().__init__(profile, parent)
        self.entry = entry

    def acceptNavigationRequest(self, url, kind, is_main_frame):
        return is_main_frame and url == self.entry

    def createWindow(self, kind):
        return None


class WorkspaceBridge(QObject):
    received = Signal(str, str)
    replied = Signal(str, str)

    def __init__(self, owner, opened, actions, payload_limits=None):
        super().__init__(owner)
        self.opened = opened
        self.actions = dict(actions)
        self.payload_limits = dict(payload_limits or {})
        self.pending = set()
        self.is_ready = False

    @Slot()
    def ready(self):
        if not self.is_ready:
            self.is_ready = True
            self.opened()

    def send(self, event, value):
        if self.is_ready and isValid(self):
            self.received.emit(event, json.dumps(value, ensure_ascii=False, allow_nan=False))

    @Slot(str, str, str)
    def request(self, token, operation, payload):
        if (not self.is_ready or not token.isascii() or not token.isdecimal()
                or not 0 < len(token) <= 12 or token in self.pending):
            return
        if len(self.pending) >= 24:
            self.replied.emit(token, json.dumps(dict(ok=False, message='请等待正在处理的操作')))
            return

        def finish(value=None, *, message=''):
            if token not in self.pending or not isValid(self):
                return
            self.pending.remove(token)
            try:
                reply = json.dumps(dict(ok=not message, value=value, message=message),
                                   ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                reply = json.dumps(dict(ok=False, message='无法显示操作结果'))
            self.replied.emit(token, reply)

        self.pending.add(token)
        try:
            action = self.actions.get(operation)
            if len(payload) > self.payload_limits.get(operation, 65536):
                finish(message='本次提交内容过多，请缩小选择范围后重试')
                return
            arguments = json.loads(payload)
            if action is None or type(arguments) is not dict:
                raise ValueError('操作参数无效')
            action(finish, **arguments)
        except (TypeError, ValueError):
            finish(message='操作参数无效')
        except Exception:
            finish(message='操作未完成，请稍后重试')


class ApprovedWorkspaceView(QWebEngineView):
    def __init__(self, parent, folder, files, shared, opened, actions, *, payload_limits=None):
        prepare_webengine_runtime()
        super().__init__(parent)
        root = Path(asset('approved_workspaces')).resolve()
        local = root / folder
        entry = QUrl.fromLocalFile(str(local / 'index.html'))
        allowed = [local / file for file in ('index.html', *files)]
        allowed.extend(root / 'shared' / file for file in shared)
        # The engine tears down a page before it can safely retire its profile.
        profile = QWebEngineProfile(QCoreApplication.instance())
        page = LocalPage(profile, entry, self)
        page.destroyed.connect(profile.deleteLater)
        self.resources = LocalResources(allowed, profile)
        profile.setUrlRequestInterceptor(self.resources)
        page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        settings = page.settings()
        for attribute in (
            QWebEngineSettings.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.JavascriptCanOpenWindows,
            QWebEngineSettings.LocalStorageEnabled,
            QWebEngineSettings.PluginsEnabled,
        ):
            settings.setAttribute(attribute, False)
        self.bridge = WorkspaceBridge(self, opened, actions, payload_limits)
        self.channel = QWebChannel(page)
        self.channel.registerObject('workspace', self.bridge)
        page.setWebChannel(self.channel)
        self.setPage(page)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setUrl(entry)
