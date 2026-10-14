"""A small Chinese document menu; Chromium navigation commands stay unavailable."""
import json
from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QMenu
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView


def external_url(value):
    url = QUrl(value)
    if (not url.isValid() or url.scheme().lower() not in {'http', 'https'} or
            not url.host() or url.userName() or url.password()):
        raise ValueError('仅支持用浏览器打开有效的网页链接')
    return url


def open_external(value):
    if not QDesktopServices.openUrl(external_url(value)):
        raise RuntimeError('无法打开默认浏览器，请复制链接后手动打开')


class WorkspaceView(QWebEngineView):
    def contextMenuEvent(self, event):
        request = self.lastContextMenuRequest()
        if request is None:
            return
        self.show_document_menu(request, event.globalPos())

    def show_document_menu(self, request, position):
        # Freeze ephemeral request values before the asynchronous JS callback.
        link = request.linkUrl().toString()
        editable, selected = request.isContentEditable(), bool(request.selectedText())
        flags = request.editFlags()
        def show(status):
            if not self.isVisible():
                return
            menu = self.make_document_menu(link, editable, selected, flags, status or {})
            if menu.actions():
                self._document_menu = menu
                menu.popup(position)
            else:
                menu.deleteLater()
        self.page().runJavaScript('window.lulu?.editStatus?.() || ({})', show)

    def make_document_menu(self, link, editable, selected, flags, status):
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest as Request
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        def add(label, callback, enabled=True):
            action = menu.addAction(label)
            action.setEnabled(enabled)
            action.triggered.connect(callback)
        def fail(message):
            self.page().runJavaScript('window.lulu?.notify(' + json.dumps(message) + ')')
        if link:
            try:
                external_url(link)
            except ValueError:
                link = ''
        if link:
            def open_link():
                try:
                    open_external(link)
                except (ValueError, RuntimeError) as exc:
                    fail(str(exc))
            add('用默认浏览器打开链接', open_link)
            add('复制链接', lambda: QGuiApplication.clipboard().setText(link))
        if editable:
            if menu.actions():
                menu.addSeparator()
            for label, name, flag in [('撤销', 'Undo', 'CanUndo'), ('重做', 'Redo', 'CanRedo')]:
                if status.get('editor'):
                    command = name.lower()
                    add(label, lambda checked=False, c=command: self.page().runJavaScript('window.lulu.editAction(' + json.dumps(c) + ')'), bool(status.get(command)))
                else:
                    add(label, lambda checked=False, n=name: self.page().triggerAction(getattr(QWebEnginePage, n)), bool(flags & getattr(Request, flag)))
            menu.addSeparator()
            for label, name, flag in [('剪切', 'Cut', 'CanCut'), ('复制', 'Copy', 'CanCopy'),
                                      ('粘贴', 'Paste', 'CanPaste'), ('粘贴为纯文本', 'PasteAndMatchStyle', 'CanPaste'),
                                      ('全选', 'SelectAll', 'CanSelectAll')]:
                add(label, lambda checked=False, n=name: self.page().triggerAction(getattr(QWebEnginePage, n)), bool(flags & getattr(Request, flag)))
        elif selected:
            if menu.actions():
                menu.addSeparator()
            add('复制', lambda: self.page().triggerAction(QWebEnginePage.Copy))
        return menu
