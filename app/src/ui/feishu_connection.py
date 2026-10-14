"""Persistent step-by-step Feishu connection page in Settings."""
from __future__ import annotations
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QFrame, QScrollArea
from ..core import feishu, secrets
from . import theme as T, icons, dialogs

_READS = set()  # Keep in-flight system credential calls alive if their page is hidden.


class ConnectionCall(QThread):
    done = Signal(bool, object)
    def __init__(self, call):
        super().__init__(); self.call = call
    def run(self):
        if self.isInterruptionRequested():return
        try: self.done.emit(True, self.call())
        except Exception: self.done.emit(False, '连接未完成，请检查系统授权或飞书应用设置。')


class FeishuConnectionPanel(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg; self.busy = False; self.loaded = False; self.worker = None
        self.authorized = False; self.saved = {}; self._oauth = None; self.attempted = False; self.disposed = False
        self.setObjectName('feishuConnection')
        self.setAttribute(Qt.WA_StyledBackground, True)
        c = T.consumer_theme(T.ACTIVE.name)
        pressed_color = QColor(c.accent_hover).darker(115).name()
        self.setStyleSheet(f"""
          QWidget#feishuConnection, QWidget#connectionBody {{background:transparent;color:{c.ink};}}
          QLabel {{background:transparent;color:{c.ink};font-family:"{c.font_body}";font-size:13px;}}
          QLabel#connectionTitle {{font-family:"{c.font_display}";font-size:26px;}}
          QLabel#secondary {{color:{c.ink_muted};font-size:12px;}}
          QLabel#connectionStatus, QLabel#stepTitle {{font-size:15px;font-weight:600;}}
          QLabel#stepNumber {{background:{c.accent_wash};color:{c.accent};border-radius:12px;font-size:12px;font-weight:600;}}
          QFrame#connectionCard {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}}
          QScrollArea {{border:0;background:transparent;}}
          QLineEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:8px;padding:9px;font-size:13px;}}
          QLineEdit:focus {{border-color:{c.accent};}}
          QPushButton {{background:{c.surface};color:{c.ink};border:2px solid {c.line};border-radius:8px;padding:8px 14px;font-size:13px;}}
          QPushButton:enabled:hover {{border-color:{c.accent};background:{c.secondary_hover};}}
          QPushButton:enabled:pressed {{border-color:{c.accent};background:{c.accent_wash};}}
          QPushButton:focus {{border:2px solid {c.accent};}}
          QPushButton#primary {{background:{c.accent};color:#ffffff;border-color:{c.accent};}}
          QPushButton#primary:enabled:hover {{background:{c.accent_hover};border-color:{c.accent_hover};}}
          QPushButton#primary:enabled:pressed {{background:{pressed_color};border-color:{pressed_color};}}
          QPushButton#primary:focus {{border-color:{c.ink};}}
          QPushButton#quiet {{background:{c.surface};border-color:{c.accent_hairline};color:{c.accent};}}
          QPushButton#quiet:enabled:hover {{background:{c.accent_wash};border-color:{c.accent};}}
          QPushButton#quiet:enabled:pressed {{background:{c.secondary_hover};border-color:{pressed_color};}}
          QPushButton#quiet:focus {{border-color:{c.accent};}}
          QPushButton:disabled, QPushButton#primary:disabled, QPushButton#quiet:disabled {{color:{c.ink_muted};background:{c.secondary};border-color:{c.line};}}
          QScrollBar:vertical {{width:6px;background:transparent;}}
          QScrollBar::handle:vertical {{background:{c.line};border-radius:3px;min-height:30px;}}
          QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        """)
        outer=QHBoxLayout(self);outer.setContentsMargins(24,20,24,20);outer.setAlignment(Qt.AlignHCenter)
        content=QWidget();content.setMaximumWidth(900)
        outer.addWidget(content,1)
        layout=QVBoxLayout(content);layout.setContentsMargins(0,0,0,0);layout.setSpacing(16)
        head=QHBoxLayout();head.setSpacing(12)
        logo=icons.icon_label('feishu',c.accent,28);logo.setFixedSize(36,40);head.addWidget(logo)
        words=QVBoxLayout();words.setSpacing(4)
        title=QLabel('飞书知识库');title.setObjectName('connectionTitle');words.addWidget(title)
        words.addWidget(self.label('完成以下设置后，即可从文稿或采集结果中「存入飞书」。'))
        head.addLayout(words,1);layout.addLayout(head)
        self.return_button=self.button('返回存入飞书',self._return_to_save,quiet=True)
        self.return_button.hide();layout.addWidget(self.return_button,0,Qt.AlignLeft)
        self._return_dialog=None
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body=QWidget();body.setObjectName('connectionBody');col=QVBoxLayout(body);col.setContentsMargins(0,0,8,0);col.setSpacing(12)
        scroll.setWidget(body);layout.addWidget(scroll,1);self.scroll=scroll

        self.details=QWidget();form=QVBoxLayout(self.details);form.setContentsMargins(0,0,0,0);form.setSpacing(12);col.addWidget(self.details)
        card,step=self.step_card('1','创建应用，填写凭据')
        step.addWidget(self.label('在飞书后台创建「企业自建应用」，从「凭证与基础信息」复制以下两项。'))
        self.console_button=self.button('打开开发者后台 ↗',lambda:self.open_url(feishu.DEV_CONSOLE_URL),quiet=True)
        step.addWidget(self.console_button,0,Qt.AlignLeft)
        grid=QGridLayout();grid.setHorizontalSpacing(16);grid.setVerticalSpacing(10)
        self.app_id=QLineEdit();self.app_id.setPlaceholderText('cli_xxxxx');self.app_id.setAccessibleName('飞书 App ID')
        self.secret=QLineEdit();self.secret.setEchoMode(QLineEdit.Password);self.secret.setPlaceholderText('输入 App Secret');self.secret.setAccessibleName('飞书 App Secret')
        for i,(name,field) in enumerate((('App ID',self.app_id),('App Secret',self.secret))):
            label=QLabel(name);label.setBuddy(field);grid.addWidget(label,i,0);grid.addWidget(field,i,1)
        grid.setColumnStretch(1,1);step.addLayout(grid)
        step.addWidget(self.label('应用凭据仅保存在本机系统钥匙串中。'));form.addWidget(card)

        card,step=self.step_card('2','开通应用权限')
        step.addWidget(self.label('在「权限管理」开通清单中的权限，用于保存文档、表格和文件。'))
        actions=QHBoxLayout();actions.setSpacing(8)
        self.permissions_button=self.button('打开权限管理 ↗',lambda:self.open_app_page('permissions'),quiet=True)
        self.scopes_button=self.button('复制权限清单',lambda:self.copy_text('\n'.join(feishu.SCOPE_BUNDLE),'已复制权限清单',self.scopes_button))
        actions.addWidget(self.permissions_button);actions.addWidget(self.scopes_button);actions.addStretch();step.addLayout(actions);form.addWidget(card)

        card,step=self.step_card('3','添加安全回调')
        step.addWidget(self.label('在「安全设置 → 重定向 URL」逐条添加以下 3 个地址。'))
        self.security_button=self.button('打开安全设置 ↗',lambda:self.open_app_page('security'),quiet=True)
        step.addWidget(self.security_button,0,Qt.AlignLeft)
        self.callback_buttons=[];self.callback_fields=[]
        for i,port in enumerate(feishu.OAUTH_PORTS,1):
            row=QHBoxLayout();row.setSpacing(8)
            address=QLineEdit(feishu.redirect_uri(port));address.setReadOnly(True);address.setAccessibleName(f'回调地址 {i}')
            button=self.button('复制',lambda:None);button.setAccessibleName(f'复制回调地址 {i}')
            button.clicked.disconnect();button.clicked.connect(lambda _,p=port,b=button:self.copy_text(feishu.redirect_uri(p),f'已复制回调地址 {p}',b))
            row.addWidget(address,1);row.addWidget(button);step.addLayout(row)
            self.callback_fields.append(address);self.callback_buttons.append(button)
        step.addWidget(self.label('配置完成后，在飞书后台发布应用版本，再回到这里连接账号。'));form.addWidget(card);col.addStretch()

        card,step=self.step_card('4','连接账号')
        self.status=QLabel('连接状态待检查');self.status.setObjectName('connectionStatus');step.addWidget(self.status)
        self.summary=self.label('完成上方设置并发布应用后，点击连接。');step.addWidget(self.summary)
        actions=QHBoxLayout();actions.setSpacing(8)
        self.login=self.button('连接飞书',self.start_login,primary=True);actions.addWidget(self.login)
        self.logout=self.button('退出登录',self.sign_out);self.logout.hide();actions.addWidget(self.logout);actions.addStretch();step.addLayout(actions)
        self.notice=self.label('');self.notice.setAccessibleName('飞书操作提示');step.addWidget(self.notice)
        layout.addWidget(card)

    def step_card(self,number,title):
        card=QFrame();card.setObjectName('connectionCard')
        layout=QVBoxLayout(card);layout.setContentsMargins(18,16,18,16);layout.setSpacing(10)
        head=QHBoxLayout();head.setSpacing(10)
        badge=QLabel(number);badge.setObjectName('stepNumber');badge.setFixedSize(24,24);badge.setAlignment(Qt.AlignCenter)
        label=QLabel(title);label.setObjectName('stepTitle');head.addWidget(badge);head.addWidget(label,1);layout.addLayout(head)
        return card,layout

    def offer_return(self,dialog):
        self._return_dialog=dialog;self.return_button.show()
        dialog.finished.connect(lambda *_:self._clear_return(dialog))

    def _clear_return(self,dialog):
        if self._return_dialog is dialog:
            self._return_dialog=None;self.return_button.hide()

    def _return_to_save(self):
        if self.busy or self._return_dialog is None:return
        dialog=self._return_dialog;self._return_dialog=None;self.return_button.hide()
        dialog.resume_from_connection()

    def _set_editable(self,editable):
        self.app_id.setEnabled(editable);self.secret.setEnabled(editable);self.return_button.setEnabled(editable)

    @staticmethod
    def label(text):
        w=QLabel(text);w.setObjectName('secondary');w.setWordWrap(True);return w
    @staticmethod
    def button(text,callback,primary=False,quiet=False):
        w=QPushButton(text);w.setAutoDefault(False);w.setObjectName('primary' if primary else 'quiet' if quiet else 'action')
        w.setCursor(Qt.PointingHandCursor);w.setFocusPolicy(Qt.StrongFocus);w.setMinimumHeight(38)
        w.setAccessibleName(text.replace(' ↗',''));w.clicked.connect(callback);return w
    def showEvent(self,event):
        super().showEvent(event)
        # Showing settings does not itself invoke a system credential prompt.
        if secrets.configuration_loaded(feishu.CONFIG_KEY) and not self.loaded:self.load_if_needed()
    def load_if_needed(self,force=False):
        if self.disposed or self.busy or (self.attempted and not force):return
        if not force and not secrets.configuration_loaded(feishu.CONFIG_KEY):return
        self.attempted=True
        self.run_call(lambda:secrets.get_config(feishu.CONFIG_KEY,require_access=True),self.loaded_config)
    def run_call(self,call,callback):
        if self.busy or self.disposed:return
        self.busy=True;self.login.setEnabled(False);self.logout.setEnabled(False);self._set_editable(False)
        self.notice.setText('正在处理连接；如果系统要求钥匙串授权，请在系统提示中完成。')
        worker=ConnectionCall(call);self.worker=worker;_READS.add(worker)
        result=[];worker.done.connect(lambda ok,value:result.append((ok,value)))
        def finish():
            self.busy=False;self.worker=None
            if self.disposed:
                _READS.discard(worker);worker.deleteLater();return
            self.login.setEnabled(True);self.logout.setEnabled(True);self._set_editable(True)
            if result:
                ok,value=result[0]
                if ok:callback(value)
                else:self.notice.setText(str(value))
            _READS.discard(worker);worker.deleteLater()
        worker.finished.connect(finish);worker.start()
    def loaded_config(self,value):
        self.saved=dict(value or {});self.loaded=True
        if not self.app_id.isModified():self.app_id.setText(self.saved.get('app_id',''))
        if not self.secret.isModified():self.secret.setText(self.saved.get('app_secret',''))
        self.authorized=bool(self.saved.get('refresh_token'))
        self.status.setText('已保存飞书授权' if self.authorized else '连接飞书账号')
        self.summary.setText('保存时会核验当前账号和目标权限。' if self.authorized else '连接后即可手动保存文稿和采集结果。')
        self.login.setText('重新连接' if self.authorized else '连接飞书');self.logout.setVisible(self.authorized)
        self.notice.setText('')
    def start_login(self):
        if self.disposed or (self.busy and self._oauth is None):return
        if self._oauth is not None:
            self._auth_ok=False;self._oauth.requestInterruption();self.login.setEnabled(False);self.notice.setText("正在停止本次连接…");return
        if not self.loaded:
            self.attempted=True
            def loaded(value):
                self.loaded_config(value);self.start_login()
            self.run_call(lambda:secrets.get_config(feishu.CONFIG_KEY,require_access=True),loaded);return
        app_id=self.app_id.text().strip();secret=self.secret.text().strip()
        import re
        if not re.fullmatch(r'cli_[A-Za-z0-9]+',app_id) or not secret:
            self.scroll.ensureWidgetVisible(self.app_id);(self.app_id if not app_id else self.secret).setFocus()
            self.notice.setText('请填写正确的 App ID 和 App Secret，再连接飞书。');return
        def save():
            return feishu.save_app_credentials(app_id,secret)
        self.run_call(save,self.authorize)
    def authorize(self,value):
        if self.disposed:return
        from .feishu_dialog import _OAuthWorker
        self.loaded_config(value);self.busy=True;self._auth_ok=False;self.login.setEnabled(True);self.login.setText('取消连接');self.logout.setEnabled(False);self._set_editable(False)
        self.notice.setText('请在浏览器中完成飞书授权。');worker=_OAuthWorker();self._oauth=worker;_READS.add(worker)
        worker.done.connect(self.authorized_result)
        def finish():
            cancelled=worker.isInterruptionRequested()
            self.busy=False;self._oauth=None;_READS.discard(worker);worker.deleteLater()
            if self.disposed:return
            self.login.setEnabled(True);self.login.setText('重新连接' if self.authorized else '连接飞书');self.logout.setEnabled(True);self._set_editable(True)
            if getattr(self,'_auth_ok',False):self.load_if_needed(force=True)
            elif cancelled:self.notice.setText('本次连接已停止，未保存新的授权。')
        worker.finished.connect(finish);worker.start()
    def authorized_result(self,ok,payload):
        if self.disposed:return
        self._auth_ok=ok;self.notice.setText('已连接飞书。' if ok else '授权未完成，请检查应用权限和回调地址后重试。')
    def sign_out(self):
        if dialogs.confirm(self,'退出飞书登录','退出后需重新连接，飞书里的内容会保留。',ok='退出登录',cancel='取消'):
            self.run_call(lambda:(feishu.logout(),secrets.get_config(feishu.CONFIG_KEY))[1],self.loaded_config)
    def open_url(self,url):
        if not QDesktopServices.openUrl(QUrl(url)):self.notice.setText('暂时无法打开浏览器，请稍后重试。')
    def open_app_page(self,kind):
        aid=self.app_id.text().strip()
        import re
        if not re.fullmatch(r'cli_[A-Za-z0-9]+',aid):
            self.notice.setText('请填写正确的 App ID，再打开应用设置。');self.scroll.ensureWidgetVisible(self.app_id);self.app_id.setFocus();return
        self.open_url(feishu.app_auth_url(aid) if kind=='permissions' else feishu.safe_url(aid))
    def copy_text(self,text,message,button=None):
        QGuiApplication.clipboard().setText(text);self.notice.setText(message)
        if button is not None:
            if button.property('copyLabel') is None:button.setProperty('copyLabel',button.text())
            button.setText('已复制')
            QTimer.singleShot(1600,button,lambda:button.setText(button.property('copyLabel')))

    def owned_workers(self):
        return tuple(w for w in (self.worker,self._oauth) if w is not None)

    def shutdown(self):
        self.disposed=True
        self.login.setEnabled(False);self.logout.setEnabled(False);self._set_editable(False)
        for worker in self.owned_workers():worker.requestInterruption()


def open_connection_settings(parent,save_dialog=None):
    """Route every connection entry to the owning main window."""
    owner=parent
    while owner is not None:
        navigate=getattr(owner,'open_feishu_connection',None)
        if callable(navigate):
            return navigate(save_dialog) is not False
        owner=owner.parentWidget()
    return False
