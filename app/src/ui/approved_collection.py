"""Approved collection v14 view with the existing local collection workflows."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from ..core.collect import export
from ..core.collect.history import HistoryError
from .approved_workspaces import ApprovedWorkspaceView
from .collection_history_dialog import CollectionHistoryDialog, _MoveRecords
from .collection_page import CollectionPage, _detect_browsers, _SHANGHAI
from .feishu_save_dialog import FeishuSaveSession, _CallWorker
from .indextts2_settings import reduced_motion


def _material_status(value):
    return 'working' if value == 'downloading' else value


_BROWSER_LOGIN_LABELS = {'已登录','已保存浏览器登录'}


class ApprovedCollection(CollectionPage):
    def __init__(self, cfg, *, browser_login=None, browser_login_active=None, browser_login_blocked=None,
                 browser_retry_blocked=None, browser_login_saving=None, browser_clear_login=None, **kwargs):
        self._visible_work_keys = ()
        self._visible_status = {}
        self._work_items = {}
        super().__init__(cfg, **kwargs)
        self.setStyleSheet('QWidget#root[workspacePage="collection"] { background: transparent; }')
        native = QWidget(self)
        native.setLayout(self.layout())
        native.hide()
        self._browser_entries = dict(_detect_browsers())
        self._browser_login = browser_login
        self._browser_clear_login = browser_clear_login
        self._browser_login_active = browser_login_active or (lambda: False)
        self._browser_login_blocked = browser_login_blocked or (lambda: '')
        self._browser_retry_blocked = browser_retry_blocked or (lambda: '')
        self._browser_login_saving = browser_login_saving or (lambda: False)
        self._workspace_link_version = 0
        self._previous_payload = None
        self._previous_works = None
        self._works_version = 0
        self._works_dirty = True
        self._moving_records = None
        self._export_pending = False
        self._export_worker = None
        self._last_feishu_payload = None
        self._poll = QTimer(self,interval=400)
        self._poll.timeout.connect(self._publish)
        self.approved = ApprovedWorkspaceView(
            self,'collection',
            ('preview.css','refinements.css','alignment.css','workspace.css','control-density.css',
             'focus-motion.css','record-management.css','embedded.css','backend-contract.js','filters.js',
             'workspace.js','record-picker.js','material.js','collection.js','feishu.js'),
            ('select-controls.css','select-controls.js','channel.js','lulu.png','paper.png'),
            self._opened,
            {
                'mode':self._mode, 'inspect_link':self._inspect_link, 'collect':self._collect,
                'stop':self._stop, 'retry_partial':self._retry_partial,
                'material_pref':self._material_pref, 'browser':self._browser,
                'login':self._login, 'clear_login':self._clear_login,
                'select_record':self._select_record, 'rename_record':self._rename_record,
                'trash_records':self._trash_records, 'asset':self._asset, 'reveal':self._reveal,
                'redownload_cover':self._redownload_cover,
                'restore_legacy':self._restore_legacy,
                'asset_detail':self._asset_detail, 'visible_works':self._visible_works,
                'manuscript':self._manuscript, 'transcribe':self._transcribe,
                'export':self._export, 'feishu':self._feishu, 'feishu_control':self._feishu_control,
            },
            # Explicit bounded selection lists can contain thousands of work IDs.
            payload_limits={action: 2_000_000 for action in ('export', 'transcribe', 'feishu')},
        )
        self.approved.setAccessibleName('主页采集')
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.approved)

    def _opened(self):
        self.approved.bridge.send('motion',reduced_motion())
        self._publish(force=True)

    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'_poll'):
            self._poll.start()
            self._publish()

    def hideEvent(self,event):
        if hasattr(self,'_poll'):
            self._poll.stop()
        super().hideEvent(event)

    def shutdown_workers(self,timeout_ms=3000):
        self._poll.stop()
        if self._moving_records and self._moving_records.isRunning():
            self._moving_records.wait()
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.requestInterruption()
            self._export_worker.wait()
        return super().shutdown_workers(timeout_ms)

    def _fill_table(self,*,status_only=False):
        # Business state remains in CollectionPage. The hidden native table
        # does not need thousands of duplicate buttons and cell widgets.
        self._work_items = {item.stable_key: item for item in self._items if item.stable_key}
        self._visible_status.clear()
        self._works_dirty = True

    def _visible_works(self, done, *, keys):
        if type(keys) is not list or len(keys) > 80 or any(type(key) is not str for key in keys):
            raise ValueError('当前作品范围无效')
        self._visible_work_keys = tuple(key for key in keys if key in self._work_items)
        self._refresh_visible_materials()
        done(True)

    def _refresh_visible_materials(self):
        if self._shutting_down or not self.isVisible() or not self._visible_work_keys:
            return
        tasks = self._work_task_states()
        statuses = {key: self._work_status(self._work_items[key], tasks)
                    for key in self._visible_work_keys if key in self._work_items}
        changed = [dict(id=key, **state) for key, state in statuses.items()
                   if state != self._visible_status.get(key)]
        self._visible_status = statuses
        if changed:
            self.approved.bridge.send('material_states', changed)

    def _on_material_progress(self,payload):
        entry = self._apply_material_progress(payload)
        if entry is None or not hasattr(self,'approved'):
            return
        key, kind = entry
        state = self._material_runtime.get(key, {}).get(kind, '')
        self.approved.bridge.send('material',dict(id=key,kind=kind,state=_material_status(state),
                                                progress=self._material_progress.get((key,kind))))

    def _work_task_states(self):
        try:
            return dict(self._collection_task_states() or {})
        except Exception:
            return {}

    def _work_status(self, item, tasks):
        key = item.stable_key
        done = key in self._transcribed or item.aweme_id in self._transcribed
        phase = getattr(tasks.get(key), 'transcript', '')
        row = dict(text='ready' if done else 'working' if phase == 'transcribing'
                   else phase if phase in {'queued','failed','paused','cancelled','interrupted','recovery_required'}
                   else 'missing')
        for kind in ('media', 'cover'):
            state, reason = self._material_cell_state(item, kind, tasks)
            row[kind] = _material_status(state)
            row[kind + 'Progress'] = self._material_progress.get((key, kind))
            row[kind + 'Reason'] = reason
        row['coverCanRedownload'] = bool(
            row['cover'] == 'ready' and item.platform == 'douyin'
            and self._cover_workflow is not None and self._cover_retry_worker is None
            and self._cover_retry_eligible(item)
        )
        return row

    def _work_rows(self):
        tasks = self._work_task_states()
        rows=[]
        for item in self._items:
            key=item.stable_key
            if not key:
                continue
            created=item.known_numeric('create_time')
            milliseconds=item.known_numeric('duration_ms')
            row=dict(
                id=key,author=item.author_name,author_name=item.author_name,title=item.title or '未命名作品',
                kind=item.content_kind,content_kind=item.content_kind,platform=item.platform,work_id=item.work_id,
                work_url=item.work_url,cover_status=item.cover_status,create_time=created,duration_ms=milliseconds,
                seconds=milliseconds//1000 if milliseconds is not None else None,
                durationText=item.duration_text if milliseconds is not None else '',
                date=item.publish_date if created else '',
                **self._work_status(item, tasks),
            )
            for field in ('like','collect','comment','share'):
                row[field]=item.known_numeric(field)
            rows.append(row)
        return rows

    def _payload(self):
        snapshot=self._history_snapshot
        records=[] if snapshot is None else [
            dict(id=row.record_id,name=row.name,author=row.author_name,count=len(row.items),
                 time=datetime.fromisoformat(row.created_at).astimezone(_SHANGHAI).strftime('%m-%d %H:%M'))
            for row in snapshot.active_records
        ]
        status=QTextDocument()
        status.setHtml(self.status.text())
        login_label, can_clear = self._login_display()
        if self._works_dirty or self._previous_works is None:
            rows=self._work_rows()
            if rows!=self._previous_works:
                self._previous_works=rows
                self._works_version+=1
            self._works_dirty=False
        return dict(
            records=records,selected=snapshot.selected_record_id if snapshot else '',
            historyVersion=snapshot.source_hash if snapshot else '',
            legacyRemoved=bool(snapshot and (snapshot.trashed_records or any(
                record.item_trash for record in snapshot.active_records))),
            running=self._collect_running(),stopping=self._stopping_collection,
            message=status.toPlainText(),link=self.input.text(),linkVersion=self._workspace_link_version,
            mode=self._selected_mode,loggedIn=login_label in _BROWSER_LOGIN_LABELS,
            loginLabel=login_label,canClearLogin=bool(can_clear and login_label!='未登录'),
            loginBusy=self._douyin_login_busy(),loginActive=self._browser_login_active(),
            loginBlockReason=self._browser_login_blocked(),
            retryBlockReason=self._browser_retry_blocked(),loginSaving=self._browser_login_saving(),
            partialRetry=getattr(self,'_partial_login_retry',None) is not None,
            browsers=list(self._browser_entries),
            browser=self._browser_name,saveMedia=self.save_media_cb.isChecked(),saveCover=self.cover_cb.isChecked(),
            works=self._previous_works,worksVersion=self._works_version,
        )

    def _publish(self,force=False):
        if not hasattr(self,'approved') or not self.approved.bridge.is_ready or self._shutting_down:
            return
        payload=self._payload()
        if force or payload!=self._previous_payload:
            unchanged_works=(self._previous_payload is not None
                and payload['worksVersion']==self._previous_payload['worksVersion']
                and payload['selected']==self._previous_payload['selected'])
            self._previous_payload=payload
            event=dict(payload)
            if unchanged_works and not force:
                event.pop('works')
            self.approved.bridge.send('snapshot',event)
        self._publish_feishu()

    def _mode(self,done,*,value):
        if value not in {'partial','full'} or self._collect_running():
            return done(message='请等待当前采集完成')
        if self._save_collection_mode(value):
            done(True)
        else:
            done(message=self.status.text())

    def _login_display(self):
        if self._douyin_login_state:
            return self._douyin_login_state()
        saved=self.clear_login_btn.isEnabled()
        return ('已登录' if saved else '未登录',saved)

    def accept_workspace_request(self,url,limit):
        super().accept_workspace_request(url,limit)
        self._workspace_link_version+=1
        self._publish()

    def _link_state(self,link):
        if type(link) is not str or len(link)>4096:
            return dict(code='invalid',message='分享文字过长，请只粘贴主页链接。')
        if not link.strip():
            return dict(code='empty',message='请先粘贴抖音公开主页链接。')
        entry=self._service.inspect(link)
        identity=entry.identity
        if identity.platform=='douyin' and entry.capability.available:
            url=urlsplit(identity.canonical_url)
            if identity.kind=='homepage' or (
                identity.kind=='unknown' and url.hostname=='v.douyin.com' and url.path not in {'','/'}
            ):
                return dict(code='ready',message='')
        if identity.platform=='xiaohongshu':
            return dict(code='blocked',message='小红书主页采集本版暂缓，已有任务仍可筛选和导出。')
        if identity.platform=='bilibili':
            return dict(code='blocked',message='B站主页采集暂不可用，已有任务仍可查看。')
        if identity.reason_code=='ambiguous_link':
            return dict(code='invalid',message='这段文字里有多个链接，请只粘贴一个抖音主页链接。')
        return dict(code='invalid',message='请检查并粘贴抖音作者主页链接或主页分享文字。')

    def _inspect_link(self,done,*,link):
        done(self._link_state(link))

    def _collect(self,done,*,link,mode):
        entry=self._link_state(link)
        if entry['code']!='ready':
            return done(message=entry['message'])
        if mode not in {'partial','full'}:
            raise ValueError('采集方式无效')
        if mode != self._selected_mode:
            return done(message='采集方式已变化，请确认后再开始')
        if self._collect_running() or self._moving_records:
            return done(message='请等待当前操作完成')
        if self._douyin_login_busy():
            return done(message='请先完成或取消登录，再开始采集。')
        self.input.setText(link)
        if mode=='full' and self._workspace_request_limit() is None and self._login_display()[0] not in _BROWSER_LOGIN_LABELS:
            return self._login(done)
        self._on_primary_action()
        done(self._payload())

    def _stop(self,done):
        if not self._collect_running() or self._stopping_collection:
            return done(self._payload())
        for worker in (self._worker,self._full_worker):
            if worker and worker.isRunning():
                worker.request_cancel()
        self._stopping_collection=True
        self.status.setText('正在停止采集，已经取得的作品会保留。')
        self._publish()
        done(self._payload())

    def _retry_partial(self,done,*,link):
        if type(link) is not str or link.strip()!=self.input.text().strip() or self._moving_records:
            return done(message='原采集链接已变化，请重新开始采集。')
        blocked=self._browser_retry_blocked()
        if blocked:
            return done(message=blocked)
        self._on_partial_login_retry(confirm=False)
        done(self._payload())

    def _material_pref(self,done,*,kind,enabled):
        if type(enabled) is not bool or kind not in {'media','cover'}:
            raise ValueError('素材选项无效')
        widget=self.save_media_cb if kind=='media' else self.cover_cb
        widget.setChecked(enabled)
        # A failed save restores the old value, so the normal change check
        # would skip the snapshot that corrects the checkbox in the web view.
        self._publish(force=widget.isChecked()!=enabled)
        done(widget.isChecked())

    def _browser(self,done,*,name):
        if name not in self._browser_entries or self._collect_running() or self._douyin_login_busy():
            return done(message='当前无法切换浏览器，请结束采集或登录后重试。')
        self._pick_browser(name,self._browser_entries[name])
        done(self._payload())

    def _login(self,done,*,cancel=False):
        if type(cancel) is not bool or self._browser_login is None:
            return done(message='专用浏览器尚未就绪，请稍后重试。')
        messages=[]
        def feedback(message):
            messages.append(message)
            self.status.setText(message)
        if not self._browser_login(self._browser_path,feedback,cancel=cancel):
            return done(message=messages[-1] if messages else '登录暂时无法开始，请查看连接和任务状态后重试。')
        done(self._payload())

    def _clear_login(self,done):
        if not self._login_display()[1] or self._login_display()[0] == '未登录':
            return done(message='当前没有已保存的抖音登录。')
        if self._browser_clear_login is None:
            return done(message='登录服务尚未就绪，请稍后重试。')
        messages=[]
        def feedback(message):
            messages.append(message)
            self.status.setText(message)
        if not self._browser_clear_login(feedback):
            return done(message=messages[-1] if messages else '暂时无法清除登录，请稍后重试。')
        done(self._payload())

    def _verify_history(self,version):
        snapshot=self._history_snapshot
        if self._history_repo is None or snapshot is None or version!=snapshot.source_hash:
            raise HistoryError('stale_source','采集记录已变化')
        return snapshot

    def _select_record(self,done,*,id,version):
        if self._collect_running() or self._moving_records:
            return done(message='请等待当前操作结束，再切换采集记录。')
        try:
            snapshot=self._verify_history(version)
            if not any(record.record_id==id for record in snapshot.active_records):
                return done(message='这份采集记录已不在列表')
            self._on_history_selected(id)
            if self._history_snapshot.selected_record_id!=id:
                return done(message=self.status.text() or '暂时无法读取这份记录')
            done(self._payload())
        except HistoryError as error:
            done(message=self._history_error_text(error.code))

    def _rename_record(self,done,*,id,name,version):
        if self._moving_records or self._collect_running():
            return done(message='请等待当前操作结束，再修改采集记录。')
        if type(name) is not str or not name.strip() or len(name)>128:
            raise ValueError('记录名称无效')
        try:
            snapshot=self._verify_history(version)
            result=self._history_repo.rename(id,name.strip(),expected_source_hash=snapshot.source_hash)
            self._apply_history_snapshot(result)
            done(self._payload())
        except HistoryError as error:
            done(message=CollectionHistoryDialog._error_text(error.code))

    def _restore_legacy(self,done,*,version):
        if self._moving_records or self._collect_running():
            return done(message='请等待当前操作结束再恢复记录。')
        try:
            snapshot=self._verify_history(version)
            count=len(snapshot.trashed_records)
            items=sum(len(record.item_trash) for record in snapshot.active_records+snapshot.trashed_records)
            if not count and not items:
                return done(self._payload())
            from . import dialogs
            if not dialogs.confirm(self,'恢复旧版保留内容',
                    f'将旧版本移除后保留的 {count} 份记录和 {items} 条作品恢复到采集列表。原作品文件保持不变。',ok='恢复到列表'):
                return done(None)
            restored=self._history_repo.restore_legacy_removed(expected_source_hash=snapshot.source_hash)
            self._apply_history_snapshot(restored,restore_selected=True)
            done(self._payload())
        except HistoryError as error:
            done(message=CollectionHistoryDialog._error_text(error.code))

    def _trash_records(self,done,*,ids,version):
        if type(ids) is not list or not ids or len(ids)>1000 or any(type(key) is not str for key in ids):
            raise ValueError('请选择采集记录')
        if self._moving_records or self._collect_running():
            return done(message='请等待当前采集结束再试')
        try:
            snapshot=self._verify_history(version)
            if not snapshot.writable or not set(ids)<={row.record_id for row in snapshot.active_records}:
                return done(message='记录已变化或暂时只读')
        except HistoryError as error:
            return done(message=self._history_error_text(error.code))
        worker=_MoveRecords(self._history_repo,tuple(dict.fromkeys(ids)),snapshot.source_hash,self)
        self._moving_records=worker
        result=[]
        worker.saved.connect(lambda snapshot:result.append((snapshot,'')))
        worker.failed.connect(lambda code:result.append((None,CollectionHistoryDialog._error_text(code))))
        def finished():
            self._moving_records=None
            worker.deleteLater()
            if self._shutting_down:
                return
            saved,message=result[0] if result else (None,'系统废纸篓操作未完成')
            if saved is None:
                return done(message=message)
            self._on_history_changed(saved)
            done(self._payload())
        worker.finished.connect(finished)
        worker.start()

    def _items_for(self,id,keys):
        current=self._history_snapshot.selected_record_id if self._history_snapshot else ''
        if id not in {current,current or 'current'} or type(keys) is not list or not keys or len(keys)>20000:
            raise ValueError('所选记录已变化，请重新选择')
        lookup=self._work_items
        if any(type(key) is not str or key not in lookup for key in keys):
            raise ValueError('所选作品已变化，请重新选择')
        return [lookup[key] for key in dict.fromkeys(keys)]

    def _transcribe(self,done,*,id,keys):
        items=self._items_for(id,keys)
        self._selected_keys={item.stable_key for item in items}
        message_before = self.status.text()
        completion = self._on_transcribe_selected(wait_for_save=True)
        if completion is None:
            return done(dict(message=self.status.text()))
        self.status.setText(message_before)

        def saved(future):
            if future.cancelled() or future.exception() is not None:
                done(message='任务未能保存，所选作品已保留')
            else:
                self.status.setText('已加入内容提取，等待点击开始。')
                done(dict(message=self.status.text()))

        completion.add_done_callback(saved)

    def _feishu(self,done,*,id,keys):
        items=self._items_for(id,keys)
        self._selected_keys={item.stable_key for item in items}
        self._on_export_bitable()
        done(True)

    def _open_feishu_save(self, records, title):
        session = FeishuSaveSession(
            self, self.cfg, records, title, 'homepage', self.status.setText, present=self._present_feishu,
        )
        def released():
            self._last_feishu_payload = None
            self.approved.bridge.send('feishu',None)
            if getattr(self,'_feishu_session',None) is session:
                self._feishu_session = None
            session.dialog.deleteLater()
            session.deleteLater()
        session.finished.connect(released)
        session.start()
        return session

    def _present_feishu(self, dialog):
        if getattr(self,'_feishu_session',None) is dialog.session and not self.isVisible():
            owner = self.window()
            if callable(getattr(owner,'_switch_page',None)):
                owner._switch_page(5)
        QTimer.singleShot(0,self,self._publish_feishu)

    @staticmethod
    def _combo_view(widget):
        return dict(labels=[widget.itemText(i) for i in range(widget.count())], index=widget.currentIndex())

    def _publish_feishu(self):
        session = getattr(self,'_feishu_session',None)
        if not session or not session.presented or not self.approved.bridge.is_ready:
            return
        dialog = session.dialog
        if dialog._connection_suspended:
            self._last_feishu_payload = None
            return
        can_submit = dialog.save_button.isEnabled() and bool(dialog.account)
        if can_submit and not dialog.busy and not dialog.result:
            try:
                dialog._selection()
            except (KeyError, ValueError):
                can_submit = False
        from ..core.feishu_mapping import SOURCE_BY_KEY
        value = dict(
            count=len(dialog.records),source=dialog.source_title.text(),
            account=dialog.account_label.text(),connected=bool(dialog.account),
            status=dialog.status.text(),busy=dialog.busy,mode=dialog.mode.currentData(),
            repeat=dialog.repeat.currentData(),url=dialog.url.text(),name=dialog.name.text(),
            locationUrl=dialog.location_url.text(),custom=bool((dialog.location.currentData() or {}).get('custom')),
            target=self._combo_view(dialog.target),table=self._combo_view(dialog.table),
            template=self._combo_view(dialog.template),location=self._combo_view(dialog.location),
            mapping=[dict(key=key,title=SOURCE_BY_KEY[key].name,**self._combo_view(widget))
                     for key,widget in dialog.map_widgets.items()],
            details=dialog.result_details.toPlainText() if dialog.result else '',
            result=bool(dialog.result),complete=bool(dialog.result and dialog.result.complete),
            canOpen=bool(dialog.result and dialog.result.url),
            canSubmit=can_submit,submitLabel=dialog.save_button.text(),
        )
        if value!=self._last_feishu_payload:
            self._last_feishu_payload=value
            self.approved.bridge.send('feishu',value)

    def _feishu_control(self, done, *, action, value=None, key=None):
        session = getattr(self,'_feishu_session',None)
        if not session or not session.presented or session.dialog._connection_suspended:
            return done(message='飞书表单已关闭')
        dialog = session.dialog
        if action=='cancel':
            dialog.reject()
            return done(True)
        if dialog.busy:
            return done(message='请等待当前飞书操作')
        if action in {'connect','read','submit','adjust','open'}:
            operation = dict(connect=dialog._connect,read=dialog._discover,submit=dialog._save,
                             adjust=dialog._adjust,open=dialog._open_result)
            operation[action]()
        elif action in {'mode','repeat'} and type(value) is int and value in (0,1):
            getattr(dialog,action).setCurrentIndex(value)
        elif action in {'url','name','location_url'} and type(value) is str and len(value)<=2048:
            input_widget = getattr(dialog,action)
            if input_widget.text()!=value:
                input_widget.setText(value)
                if action=='url':
                    dialog._url_edited()
        else:
            combo = (dialog.map_widgets.get(key) if action=='mapping'
                     else getattr(dialog,action) if action in {'table','target','template','location'} else None)
            if combo is None or type(value) is not int or not 0<=value<combo.count():
                raise ValueError('飞书表单选项已变化')
            combo.setCurrentIndex(value)
        if getattr(self,'_feishu_session',None) is session:
            self._publish_feishu()
        done(True)

    def _export(self,done,*,id,keys,format):
        if format not in {'csv','xlsx'}:
            raise ValueError('导出格式无效')
        if self._export_pending:
            return done(message='正在导出，请等待本次操作结束')
        items=self._items_for(id,keys)
        self._export_pending = True
        try:
            target,_=QFileDialog.getSaveFileName(self,'导出采集表格','主页采集.'+format,f'{format.upper()} 文件 (*.{format})')
        finally:
            self._export_pending = False
        if not target or self._shutting_down:
            return done(dict(saved=False))
        path = Path(target).with_suffix('.'+format)
        writer=export.write_csv if format=='csv' else export.write_xlsx

        def write():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(prefix='.lulu-export-', suffix=path.suffix,
                                                 dir=path.parent, delete=False) as staging:
                    temporary = Path(staging.name)
                writer(items, temporary, self._transcript_text_map())
                if worker.isInterruptionRequested():
                    return dict(saved=False)
                os.replace(temporary, path)
                return dict(saved=True,count=len(items))
            except OSError:
                raise ValueError('表格未能保存，请检查目标位置和可用空间后重试。') from None
            except ValueError:
                raise
            except Exception:
                raise ValueError('表格未能生成，原文件已保留，请稍后重试。') from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        worker = _CallWorker(write)
        worker.setParent(self)
        self._export_pending = True
        self._export_worker = worker
        outcome = []
        worker.result.connect(lambda ok, value: outcome.append((ok, value)))

        def finished():
            self._export_worker = None
            self._export_pending = False
            worker.deleteLater()
            if not self._shutting_down:
                ok, value = outcome[0] if outcome else (False, '导出未完成，请重试')
                if ok:
                    done(value)
                else:
                    done(message=value)

        worker.finished.connect(finished)
        worker.start()

    def _asset(self,done,*,id,kind,state):
        if kind not in {'media','cover'} or state not in {'','ready'}:
            raise ValueError('素材参数无效')
        self._activate_material_cell(id,kind,state)
        self._works_dirty=True
        done(self._payload())

    def _asset_detail(self,done,*,id,kind):
        path = self._asset_path(id,kind)
        if path is None:
            self._works_dirty=True
            self._publish()
            return done(message='这份素材已不在保存位置')
        done(dict(name=path.name))

    def _redownload_cover(self, done, *, id):
        item = next((row for row in self._items if row.stable_key == id), None)
        if (item is None or item.platform != 'douyin' or self._cover_workflow is None
                or not self._cover_retry_eligible(item)):
            raise ValueError('这条作品当前没有可重新下载的高清封面来源')
        state, _reason = self._material_cell_state(item, 'cover', self._work_task_states())
        if state != 'ready' or self._cover_retry_worker is not None:
            return done(message='封面状态已变化或正在保存，请稍后再试')
        self._retry_cover_item(item)
        self._works_dirty = True
        done(self._payload())

    def _asset_path(self,id,kind):
        item=next((item for item in self._items if item.stable_key==id),None)
        if item is None or kind not in {'media','cover'}:
            raise ValueError('素材不在当前记录')
        return self._managed_media_file(item) if kind=='media' else self._managed_cover_file(item)

    def _reveal(self,done,*,id,kind):
        path=self._asset_path(id,kind)
        if path is None:
            self._works_dirty=True
            self._publish()
            return done(message='这份素材已不在保存位置')
        if subprocess.run(['open','-R',str(path)],check=False).returncode:
            return done(message='Finder 未能打开保存位置')
        done(True)

    def _manuscript(self,done,*,id):
        if not any(item.stable_key==id for item in self._items):
            raise ValueError('文稿不在当前记录')
        text=self._transcript_text_map().get(id,'')
        done(dict(text=text) if text else None,message='' if text else '未找到已保存文稿')
