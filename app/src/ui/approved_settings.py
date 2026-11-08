"""Approved settings v7 over the existing local preferences and services."""
from __future__ import annotations

from pathlib import Path
import shlex
from uuid import uuid4

from PySide6.QtCore import QMicrophonePermission, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from ..core import feishu, secrets
from ..version import __build__, __version__
from .. import edition
from .approved_workspaces import ApprovedWorkspaceView
from .cli_info import (
    agent_cli_path, AGENT_SKILL_REPOSITORY_URL, CLI_FEATURES, CLI_UNAVAILABLE,
    agent_skill_install_prompt,
)
from .indextts2_settings import reduced_motion
from .settings_page import (
    AUTHOR_URL, REPOSITORY_URL, SettingsPage, StorageRelocateWorker, _PATH_OPTIONS,
    _SettingsReadWorker, _start_settings_read,
)
from .skills_page import LocalSkillDropZone
from .update_ui import _state_presentation, _development_update

_PREFS = {
    'font': 'interface_font_percent', 'awake': 'prevent_sleep_during_tasks',
    'notification': 'notification_policy', 'completed': 'notify_on_completion',
    'failed': 'notify_on_failure', 'attention': 'notify_on_attention',
    'sound': 'notification_sound_enabled', 'autoUpdate': 'auto_update_enabled',
    'confirmExit': 'confirm_exit_during_tasks', 'completionAction': 'completion_action',
}
_PATHS = {
    field: (title, hint, 'voices' if field == 'voice_library_dir' else 'models' if field == 'models_dir' else 'export', icon)
    for (field, title, hint, _), icon in zip(_PATH_OPTIONS, ('document', 'download', 'mic', 'audio', 'audio', 'archive'))
}
_PERMISSION_PANES = {
    'notifications': 'com.apple.Notifications-Settings.extension',
    'microphone': 'com.apple.preference.security?Privacy_Microphone',
    'system': 'com.apple.preference.security?Privacy_ScreenCapture',
}
_EXTERNAL = {REPOSITORY_URL, AUTHOR_URL, AGENT_SKILL_REPOSITORY_URL, feishu.DEV_CONSOLE_URL, 'https://open.feishu.cn/app'}


def _update(snapshot):
    ui = snapshot.ui_state
    tone, title, detail, label, _ = _state_presentation(snapshot)
    if _development_update(snapshot):
        return dict(state='disabled', presentation=[title,detail,'','','info'], progress=0, notes='')
    if ui in {'idle','up_to_date'}:
        detail = '你可以手动检查，也可以让 Lulu 定期查看版本信息。'
    if ui == 'update_available' and snapshot.latest_version:
        title += ' ' + snapshot.latest_version
    state, action, icon = {
        'up_to_date': ('current','check-update','check'),
        'update_available': ('available','download-update','download'),
        'downloading': ('downloading','pause-update','download'),
        'download_paused': ('paused','download-update','download'),
        'download_error': ('failure','download-update','refresh'),
        'ready': ('ready','install-update','check'),
        'ready_blocked': ('ready','','check'),
        'checking_manual': ('checking','','refresh'),
        'checking_auto': ('checking','','refresh'),
        'preparing': ('checking','','refresh'),
        'verifying': ('checking','','refresh'),
        'pausing_download': ('checking','','refresh'),
        'installed': ('completed','check-update','check'),
        'install_error_rollback': ('failure','download-update','refresh'),
        'manual_required': ('failure','manual-update','download'),
        'rollback_failed': ('failure','manual-update','download'),
    }.get(ui, ('failure' if tone in {'error','warning'} else 'current','check-update','refresh'))
    progress = min(100, round(100 * snapshot.downloaded / snapshot.total)) if snapshot.total else 0
    return dict(state=state, presentation=[title,detail,label,action,icon], progress=progress,
                notes=snapshot.notes if ui in {'update_available','ready','ready_blocked'} else '')


class SettingsView(ApprovedWorkspaceView):
    local_drop = Signal(str)

    @staticmethod
    def _drop_path(event):
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            raise ValueError('请拖入一个本地技能文件或文件夹')
        return LocalSkillDropZone.local_source(urls[0].toLocalFile())

    def dragEnterEvent(self, event):
        try:
            self._drop_path(event)
        except (ValueError, OSError):
            event.ignore()
        else:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        try:
            source = self._drop_path(event)
        except (ValueError, OSError):
            event.ignore()
        else:
            self.local_drop.emit(source)
            event.acceptProposedAction()


class ApprovedSettings(SettingsPage):
    permission_receipt = Signal(object)
    permission_status = Signal(object)
    live_location_requested = Signal()
    sidebar_width_changed = Signal(int)

    def __init__(self, cfg, **kwargs):
        self._pending_font = None
        super().__init__(cfg, **kwargs)
        self.setStyleSheet(self.styleSheet() + '\nQWidget#root[workspacePage="settings"] { background: transparent; }')
        native = QWidget(self)
        native.setLayout(self.layout())
        native.hide()
        self._folder_offer = None
        self._skill_worker = None
        self._skill_tokens = set()
        self._permission_request = None
        self._checking_notification = False
        self._last_feishu = None
        self._feishu_poll = QTimer(self, interval=250)
        self._feishu_poll.timeout.connect(self._publish_feishu)
        self.permission_receipt.connect(self._finish_permission)
        self.permission_status.connect(self._publish_permission)
        self.approved = SettingsView(
            self, 'settings',
            ('preview.css','controls.css','embedded.css','controls.js','settings.js',
             'assets/contact.jpg'),
            ('channel.js','lulu.png','paper.png',), self._opened,
            {
                'category':self._category, 'sidebar_width':self._sidebar_width,
                'preferences':self._preferences,
                'preference':self._preference, 'font_preview':self._font_preview,
                'copy':self._copy, 'external':self._external, 'permission':self._permission,
                'permission_status':self._permission_status,
                'live_location':self._live_location, 'retry_inheritance':self._retry_inheritance,
                'choose_directory':self._choose_directory, 'commit_directory':self._commit_directory,
                'stop_directory':self._stop_directory,
                'skills':self._skills, 'skill_detail':self._skill_detail, 'choose_skill':self._choose_skill,
                'skill_enable':self._skill_enable, 'skill_preferences':self._skill_preferences,
                'skill_install':self._skill_install, 'skill_uninstall':self._skill_uninstall,
                'skill_import':self._skill_import, 'check_cli':self._check_cli, 'update':self._update_action,
                'feishu_load':self._feishu_load,
                'feishu_connect':self._feishu_connect, 'feishu_disconnect':self._feishu_disconnect,
                'feishu_cancel':self._feishu_cancel, 'feishu_return':self._feishu_return,
            },
        )
        self.approved.setProperty('usesLocalFontScale', True)
        self.approved.setAccessibleName('偏好设置')
        self.approved.local_drop.connect(self._dropped_skill)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.approved)
        if self.update_controller:
            self.update_controller.changed.connect(self._publish_update)
        if self._inheritance_controller:
            self._inheritance_controller.changed.connect(self._publish_inheritance)

    def _sidebar_width(self, done, *, width):
        if type(width) is int and 0 <= width <= self.approved.width():
            self.sidebar_width_changed.emit(width)
        done()

    def _prefs(self):
        return {
            key: getattr(self.cfg, field, 100 if key == 'font' else 'never' if key == 'notification' else False)
            for key, field in _PREFS.items()
        }

    def _paths(self):
        return [dict(id=id,title=title,hint=hint,kind=kind,icon=icon,path=str(self._effective_directory(id)))
                for id,(title,hint,kind,icon) in _PATHS.items()]

    @staticmethod
    def _managed():
        from ..core import library, models_manager
        from ..core.agent_workspace.skills import skill_root
        root = Path(library.LIBRARY_DIR)
        return [
            ('文稿与版本记录','文稿正文、分类、历史版本与归档记录。',str(root)),
            ('库内音频','Lulu 生成的音频。',str(root/'audio')),
            ('AI 对话','文稿旁的会话、消息与草稿。',str(root/'.workspace')),
            ('主页采集资料','采集记录、封面和下载的音视频。',str(models_manager.app_data_dir()/'collection')),
            ('直播录像','位置在直播录制中管理。',''),
            ('已安装技能','在技能设置中安装或卸载。',str(skill_root())),
        ]

    def _opened(self):
        bridge = self.approved.bridge
        bridge.send('motion', reduced_motion())
        bridge.send('initial',dict(
            page=self.current_category,prefs=self._prefs(),paths=self._paths(),managed=self._managed(),
            meta=dict(version=__version__,build=__build__,features=CLI_FEATURES,unavailable=CLI_UNAVAILABLE,
                      repository=AGENT_SKILL_REPOSITORY_URL,cliPath=agent_cli_path(),experience=edition.is_experience(),
                      cliCommand=shlex.quote(agent_cli_path()),
                      installPrompt=agent_skill_install_prompt(AGENT_SKILL_REPOSITORY_URL),
                      scopes='\n'.join(feishu.SCOPE_BUNDLE),
                      callbacks=[feishu.redirect_uri(port) for port in feishu.OAUTH_PORTS]),
        ))
        if self.update_controller:
            self._publish_update(self.update_controller.snapshot)
        self._publish_inheritance()
        if self.current_category == 'feishu':
            self._publish_feishu()

    def set_category(self, key, *, notify_view=True):
        if key == 'skills' and hasattr(self, 'approved'):
            # The visible workspace owns the skill read. Constructing the hidden
            # legacy page would start a second independent read of the same Core.
            self.current_category = key
            self.category_changed.emit(key)
        else:
            super().set_category(key)
        if hasattr(self, 'approved') and key == self.current_category:
            if notify_view:
                self.approved.bridge.send('navigate',key)
            if key == 'feishu':
                self._last_feishu = None
                self._publish_feishu()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, '_feishu_poll'):
            self._feishu_poll.start()

    def hideEvent(self, event):
        if hasattr(self, '_feishu_poll'):
            self._feishu_poll.stop()
        super().hideEvent(event)

    def _category(self, done, *, page):
        if page not in self.category_order or self.storage_operation_active():
            return done(message='请等待位置切换完成')
        # The requesting view navigates after this acknowledgement. Echoing a
        # navigate event first starts its asynchronous page load twice.
        self.set_category(page, notify_view=False)
        done(True)

    def _preferences(self, done):
        done(self._prefs())

    def _font_preview(self, done, *, percent):
        if self._settings_closing or type(percent) is not int or not 90 <= percent <= 130:
            raise ValueError('字号超出范围')
        self._pending_font = percent
        self._font_timer.start()
        self.font_preview.emit(percent)
        done(True)

    def _persist_font(self) -> bool:
        self._font_timer.stop()
        percent, self._pending_font = self._pending_font, None
        if percent is None:
            return True

        def saved(_value=None, *, message=''):
            if message:
                self.approved.bridge.send('font_rollback', self.cfg.interface_font_percent)
                self.approved.bridge.send('notice', message)

        return self._store_pref(saved, 'interface_font_percent', percent)

    def flush_font(self) -> bool:
        return self._persist_font()

    def _preference(self, done, *, key, value):
        if key == 'autoUpdate' and edition.is_experience():
            return done(message='开发体验版使用单独安装包更新，无需自动检查。')
        field = _PREFS.get(key)
        if field is None or self._settings_closing:
            raise ValueError('设置无效')
        if key == 'font':
            if type(value) is not int or not 90 <= value <= 130:
                raise ValueError('字号无效')
            self._pending_font = None
            self._font_timer.stop()
        elif key == 'notification':
            if value not in {'always','inactive','never'}:
                raise ValueError('通知时机无效')
            previous, self._permission_request = self._permission_request, None
            if previous is not None:
                previous[0](True)
            if value != 'never' and value != self.cfg.notification_policy:
                if not self.notification_service:
                    return done(message='暂时无法检查通知权限，请稍后重试')
                self._permission_request = (done,value)
                if self._checking_notification:
                    return
                self._checking_notification = True
                try:
                    self.notification_service.request_enable(self.permission_receipt.emit)
                except Exception:
                    self._permission_request = None
                    self._checking_notification = False
                    done(message='暂时无法检查通知权限')
                return
        elif key == 'completionAction':
            if value not in {'stay','open_result'}:
                raise ValueError('结果打开方式无效')
        elif type(value) is not bool:
            raise ValueError('开关值无效')
        self._store_pref(done, field, value)

    def _store_pref(self, done, field, value):
        old = getattr(self.cfg, field)
        if old != value:
            setattr(self.cfg, field, value)
            try:
                saved = self.cfg.save()
            except Exception:
                saved = False
            if not saved:
                setattr(self.cfg, field, old)
                if field == 'interface_font_percent':
                    self.font_preview.emit(old)
                done(message='设置没有写入本机，已恢复原选择')
                return False
            self.preferences_changed.emit(field)
        if field == 'interface_font_percent':
            self.font_preview.emit(value)
        done(True)
        return True

    def _finish_permission(self, result):
        self._checking_notification = False
        pending, self._permission_request = self._permission_request, None
        if pending is None or self._settings_closing:
            return
        done,value = pending
        if result.granted:
            self._store_pref(done,'notification_policy',value)
        else:
            done(message='macOS 尚未允许 Lulu 发送通知，原设置已保留')

    def _permission(self, done, *, id):
        self._open_permission(_PERMISSION_PANES[id])
        done(True)

    def _permission_status(self, done):
        microphone = QGuiApplication.instance().checkPermission(QMicrophonePermission())
        states = {Qt.PermissionStatus.Granted:'authorized', Qt.PermissionStatus.Denied:'denied',
                  Qt.PermissionStatus.Undetermined:'not_determined'}
        done(dict(microphone=states.get(microphone,'unknown')))
        if self.notification_service:
            self.notification_service.check_authorization(self.permission_status.emit)

    def _publish_permission(self, value):
        self.approved.bridge.send('permission_status', value.value)

    def _live_location(self, done):
        self.live_location_requested.emit()
        done(True)

    def _publish_inheritance(self):
        controller = self._inheritance_controller
        if controller is not None:
            self.approved.bridge.send('inheritance',dict(
                active=controller.active,state=controller.state,error=controller.error))

    def _retry_inheritance(self, done):
        controller = self._inheritance_controller
        if controller is None or controller.active or controller.state!='failed':
            return done(message='没有可重试的旧版资料检查')
        controller.retry()
        done(True)

    def _copy(self, done, *, text):
        if type(text) is not str or len(text) > 40000:
            raise ValueError('复制内容超出范围')
        QGuiApplication.clipboard().setText(text)
        done(True)

    def _external(self, done, *, url):
        if url not in _EXTERNAL:
            raise ValueError('链接不在设置入口中')
        if not QDesktopServices.openUrl(QUrl(url)):
            return done(message='无法打开浏览器')
        done(True)

    def _choose_directory(self, done, *, id):
        if id not in _PATHS or self.storage_operation_active():
            raise ValueError('目录选项无效')
        old = self._effective_directory(id)
        target = QFileDialog.getExistingDirectory(self,'选择保存文件夹',str(old))
        if not target or Path(target).expanduser() == old:
            return done()
        token = uuid4().hex
        self._folder_offer = token,id,old,Path(target).expanduser()
        done(dict(token=token,path=target))

    def _commit_directory(self, done, *, token):
        if not self._folder_offer or token != self._folder_offer[0]:
            return done(message='请重新选择目标位置')
        _,id,old,target = self._folder_offer
        if old != self._effective_directory(id) or self.storage_operation_active():
            return done(message='保存位置已经变化，请重新选择')
        if id not in {'voice_library_dir','models_dir'}:
            def saved(value=None, *, message=''):
                if value:
                    self._folder_offer = None
                done(dict(path=str(target)) if value else None,message=message)
            self._store_pref(saved,id,str(target))
            return
        if self._external_storage_busy():
            return done(message='有任务或模型安装正在进行，请在结束后重试')
        worker = StorageRelocateWorker(id,old,target,self.cfg,self)
        self._storage_workers[id] = worker
        self._storage_activity = True
        self.storage_activity_changed.emit(True)
        outcome = []
        worker.progress.connect(lambda message,fraction: self.approved.bridge.send(
            'directory_progress', dict(token=token,message=message,fraction=fraction)))
        worker.succeeded.connect(lambda field,path: outcome.append((path,'')))
        worker.failed.connect(lambda field,error,recovered,old,new: outcome.append(('',error)))
        def finish():
            self._storage_workers.pop(id,None)
            self._storage_activity = False
            self.storage_activity_changed.emit(False)
            worker.deleteLater()
            path,message = outcome[0] if outcome else ('','迁移任务未完成')
            if path:
                self._folder_offer = None
                self.preferences_changed.emit(id)
                done(dict(path=path))
            else:
                done(message=message)
        worker.finished.connect(finish)
        try:
            worker.start()
        except Exception:
            finish()

    def _stop_directory(self, done, *, token):
        if not self._folder_offer or token != self._folder_offer[0]:
            return done(message='没有可停止的迁移')
        worker = self._storage_workers.get(self._folder_offer[1])
        if worker is None:
            return done(message='迁移已经结束')
        worker.requestInterruption()
        done(True)

    def _run_skill(self, done, operation, project, **args):
        if self._skill_worker is not None or self._settings_closing:
            return done(message='请等待正在处理的技能操作')
        from ..core.agent_workspace.client import dispatch
        def run():
            try:
                return True, dispatch(operation,**args)
            except Exception as error:
                return False, str(error)
        worker = _SettingsReadWorker(run)
        self._skill_worker = worker
        def finish(ok,result):
            if self._settings_closing:
                return
            if not ok or not result[0]:
                done(message=result[1] if ok else '暂时无法读取技能')
            else:
                try:
                    done(project(result[1]))
                except (KeyError,TypeError,ValueError):
                    done(message='无法显示技能操作结果')
        def settled():
            # A result is emitted before run() returns. Keep ownership and the
            # operation guard until QThread has actually finished.
            if self._skill_worker is worker:
                self._skill_worker = None
            worker.deleteLater()
        worker.done.connect(finish)
        worker.finished.connect(settled)
        _start_settings_read(worker)

    def _skills(self, done):
        def project(result):
            return dict(entries=[
                dict(name=row['name'],title=row['title'],version=row['version'],origin=row['origin'],
                     installed=row['installed'],enabled=row['enabled'],automatic=row.get('automatic',True),
                     allowAutomatic=row.get('allow_automatic',row.get('automatic',True)),
                     summary=row.get('summary') or row.get('description',''))
                for row in result['entries']
            ])
        self._run_skill(done,'skill_list',project)

    def _skill_detail(self, done, *, id, reference):
        self._run_skill(done,'skill_details',lambda data: {key:data[key] for key in ('files','text','config')},
                        name=id,reference=reference)

    def _choose_skill(self, done):
        path, _ = QFileDialog.getOpenFileName(self,'选择本地技能','','本地技能 (*.zip SKILL.md)')
        if not path:
            return done()
        self._inspect_skill(done,path)

    def _dropped_skill(self, path):
        if self.current_category != 'skills':
            return
        def reply(value=None, *, message=''):
            if message:
                self.approved.bridge.send('notice',message)
            elif value:
                self.approved.bridge.send('skill_import',value)
        self._inspect_skill(reply,path)

    def _inspect_skill(self, done, path):
        source = LocalSkillDropZone.local_source(path)
        def project(result):
            entries = result['entries']
            self._skill_tokens = {row['token'] for row in entries if not row['reserved'] and not row['conflict']}
            return [{key:row.get(key) for key in ('token','title','description','conflict','reserved')} for row in entries]
        self._run_skill(done,'skill_inspect',project,source=source)

    def _skill_enable(self, done, *, id, enabled):
        self._run_skill(done,'skill_configure',lambda _:True,name=id,enabled=enabled)

    def _skill_preferences(self, done, *, id, config, automatic):
        self._run_skill(done,'skill_configure',lambda _:True,name=id,config=config,automatic=automatic)

    def _skill_install(self, done, *, id):
        self._run_skill(done,'skill_install',lambda _:True,name=id)

    def _skill_uninstall(self, done, *, id):
        self._run_skill(done,'skill_uninstall',lambda _:True,name=id)

    def _skill_import(self, done, *, id):
        if id not in self._skill_tokens:
            return done(message='请重新检查技能包')
        self._run_skill(done,'skill_install',lambda _:True,token=id)

    def _check_cli(self, done):
        def complete(ok,value):
            states = {'ready':'ready', 'not_installed':'normal', 'update_available':'update_available'}
            done(dict(state=states.get(value,'failure') if ok else 'failure'))
        worker = _SettingsReadWorker(self._agent_skill_status_provider)
        worker.done.connect(complete)
        worker.finished.connect(worker.deleteLater)
        _start_settings_read(worker)

    def _publish_update(self, snapshot):
        self.approved.bridge.send('update',_update(snapshot))

    def _update_action(self, done, *, action):
        controller = self.update_controller
        if controller is None:
            return done(message='更新服务暂不可用')
        current = _update(controller.snapshot)['presentation'][3]
        expected = 'install-update' if action == 'install-confirm' else action
        if current != expected:
            return done(message='更新状态已变化，请重新检查')
        if action=='check-update':
            if controller.snapshot.ui_state == 'installed':
                controller.acknowledge()
            else:
                controller.check(manual=True)
        elif action=='download-update':
            controller.download()
        elif action=='pause-update':
            controller.cancel()
        elif action=='install-confirm' and controller.install_gate()=='ok':
            self.install_requested.emit()
        elif action=='manual-update':
            from .update_ui import MANUAL_DOWNLOAD_URL
            self.manual_download.emit(MANUAL_DOWNLOAD_URL)
        else:
            return done(message='当前无法安装更新')
        done(True)

    def _publish_feishu(self):
        if self.current_category != 'feishu' or not hasattr(self,'feishu_connection'):
            return
        panel = self.feishu_connection
        value = dict(connected=panel.authorized,expired=feishu.needs_reauthorization(panel.saved),
                     verified=feishu.authorization_verified(panel.saved),
                     loaded=panel.loaded,
                     appId=panel.app_id.text(),hasSecret=bool(panel.secret.text()),
                     busy=panel.busy, notice=panel.notice.text(), canReturn=bool(panel._return_dialog))
        if value != self._last_feishu:
            self._last_feishu = value
            self.approved.bridge.send('feishu',value)

    def _feishu_load(self, done):
        if self.current_category != 'feishu':
            return done(message='请先打开飞书连接页')
        self.feishu_connection.load_if_needed(force=True)
        self._publish_feishu()
        done(True)

    def _feishu_connect(self, done, *, appId, secret):
        if self.current_category != 'feishu' or type(appId) is not str or type(secret) is not str or len(appId)>128 or len(secret)>4096:
            raise ValueError('飞书配置无效')
        panel = self.feishu_connection
        if panel.busy:
            return done(message='飞书连接正在处理')
        if appId:
            panel.app_id.setText(appId)
            panel.app_id.setModified(True)
        if secret:
            panel.secret.setText(secret)
            panel.secret.setModified(True)
        panel.start_login()
        self._publish_feishu()
        done(True)

    def _feishu_disconnect(self, done):
        panel = self.feishu_connection
        if panel.busy:
            return done(message='飞书连接正在处理')
        panel.run_call(lambda:(feishu.logout(),secrets.get_config(feishu.CONFIG_KEY))[1],panel.loaded_config)
        done(True)

    def _feishu_cancel(self, done):
        if self.feishu_connection._oauth:
            self.feishu_connection.start_login()
        done(True)

    def _feishu_return(self, done):
        self.feishu_connection._return_to_save()
        done(True)
