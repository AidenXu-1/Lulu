"""One bounded save surface shared by tasks, manuscripts and homepage selections."""
from __future__ import annotations
from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QLineEdit, QComboBox, QWidget, QCheckBox
from ..core import feishu, feishu_mapping as mapping, feishu_save as saving
from .feishu_save_surface import SaveSurface, Choice as _Choice


class _CallWorker(QThread):
    result=Signal(bool,object)
    progress=Signal(str)
    def __init__(self,call):
        super().__init__(); self.call=call
    def request_cancel(self):
        self.requestInterruption()
        if hasattr(self,"cancel_callback"): self.cancel_callback()
    def run(self):
        try: self.result.emit(True,self.call())
        except Exception as exc:
            safe=isinstance(exc,(ValueError,feishu.FeishuApiError,feishu.FeishuTransportError))
            self.result.emit(False,str(exc) if safe else '飞书操作未完成，请检查连接后重试。')


class FeishuSaveSession(QObject):
    done=Signal(bool,object)
    finished=Signal()
    def __init__(self,parent,cfg,records,title,context,toast=None):
        super().__init__(parent)
        self.dialog=FeishuSaveDialog(parent,cfg,records,title,context,self)
        self.toast=toast
        self.dialog.finished.connect(self.finished)
    def start(self): self.dialog.open()
    def isRunning(self): return self.dialog.isVisible() or self.dialog.busy or self.dialog._connection_suspended
    def request_cancel(self): self.dialog.reject()
    def isFinished(self): return not self.isRunning()


class FeishuSaveDialog(SaveSurface, QDialog):
    def __init__(self,parent,cfg,records,title,context,session):
        super().__init__(parent)
        self.cfg=cfg; self.records=tuple(records); self.session=session; self.context=context
        self._connection_suspended=False
        self.busy=False; self._worker=None; self._closing=False; self._cancel=False
        self.account={}; self.discovery={}; self.fields=[]; self.map_widgets={}; self.saved_map={}
        self.operation_id=str(uuid4()); self._last_selection=None; self.result=None; self._restored=False
        self.setWindowTitle('存入飞书'); self.setWindowModality(Qt.WindowModal)
        self._build_surface(title)
        self._load_account()

    def _set_busy(self,busy):
        self.busy=busy;self.body.setEnabled(not busy);self.save_button.setEnabled(not busy and bool(self.account));self.connect_button.setEnabled(not busy and self.result is None)
        self.adjust_button.setEnabled(not busy)
        self.cancel_button.setText('停止' if busy else ('关闭' if self.result else '取消'))
    def _run(self,call,callback,on_error=None):
        if self.busy:return
        self._set_busy(True)
        worker=_CallWorker(call);self._worker=worker;worker.setParent(self);worker.cancel_callback=lambda:setattr(self,"_cancel",True);box=[]
        worker.result.connect(lambda ok,payload:box.append((ok,payload)))
        worker.progress.connect(self.status.setText)
        def finished():
            self._set_busy(False);self._worker=None
            closing=self._closing;self._closing=False
            if box:
                ok,payload=box[0]
                if ok and (not closing or isinstance(payload,saving.SaveResult)):callback(payload)
                elif not ok:
                    self.status.setText(str(payload))
                    if on_error:on_error(str(payload))
            if closing and self.result is None:super(FeishuSaveDialog,self).reject()
            worker.deleteLater()
        worker.finished.connect(finished);worker.start()
    def _connect(self):
        if self.busy or self.result is not None:return
        from .feishu_connection import open_connection_settings
        self._connection_account_id=self.account.get('account_id')
        if not open_connection_settings(self,self):
            self.status.setText('请从左侧「飞书知识库」进入；如有暂存的保存表单，请先返回完成或关闭。')

    def resume_from_connection(self):
        self._connection_suspended=False;self.open()
        previous=getattr(self,'_connection_account_id',None)
        self.account={};self.account_label.setText('正在检查连接…')
        def got(info):
            if previous and info.get('account_id')==previous:
                self.account=info;self.account_label.setText(info['name'])
                self.save_button.setEnabled(True);self.status.setText('已返回，填写内容已保留。')
            else:
                self._load_account(info)
        self._run(saving.account_info,got,on_error=lambda _:self.account_label.setText('未连接 · 请检查设置'))

    def _load_account(self,info=None):
        self.account={};self._restored=False;self.account_label.setText('正在检查连接…')
        self._invalidate();self.url.clear()
        self.location.blockSignals(True);self.location.clear()
        self.location.addItem('云文档默认位置', {})
        self.location.addItem('使用其他文件夹或知识库…', {'custom': True})
        self.location.blockSignals(False);self.location_url.clear()
        self.target.blockSignals(True);self.target.clear();self.target.addItem('粘贴其他表格链接…','');self.target.blockSignals(False)
        def got(info):
            self.account=info;self.account_label.setText(info['name']);self.save_button.setEnabled(True)
            self.target.blockSignals(True);self.target.clear();self.target.addItem('粘贴其他表格链接…','')
            last = getattr(self.cfg, 'feishu_last_save', {}) or {}
            if not isinstance(last, dict) or last.get('account_id') != info['account_id']: last = {}
            table = last.get('table') or {}
            if table.get('url'):
                self.target.addItem(table.get('name') or '上次使用的表格', dict(table))
            self.target.blockSignals(False)
            if not self._restored:
                self._restored = True
                self.format.setCurrentIndex(1 if last.get('format') == 'document' and self.context != 'homepage' else 0)
                location = last.get('location') or {}
                if location:
                    self.location.insertItem(1, location.get('name') or '上次使用的位置', location)
                    self.location.setCurrentIndex(1)
                if table.get('url'):
                    self.target.setCurrentIndex(1)
            self.status.setText('确认目标后存入；下次会自动带出本次位置。');self._layout()
        if info is None:self._run(saving.account_info,got,on_error=lambda _:self.account_label.setText('未连接 · 请检查设置'))
        else:got(info)
    def _invalidate(self,*_):
        self.discovery={};self.fields=[];self.saved_map={}
        self.table.blockSignals(True);self.table.clear();self.table.addItem('请先读取表格','');self.table.blockSignals(False)
        self._clear_mapping();self._set_mapping_expanded(False)
        self.mapping_summary.setText('链接已变化，请重新读取表格。');self._layout()
    def _url_edited(self,*_):
        self.target.blockSignals(True);self.target.setCurrentIndex(0);self.target.blockSignals(False)
        self._invalidate()

    def _target_changed(self,*_):
        item=self.target.currentData()
        self._invalidate()
        self.url.setText(item.get('url','') if isinstance(item,dict) else '')
        if isinstance(item,dict):self.saved_map=item.get('field_map') or {};self._discover()
    def _discover(self):
        url=self.url.text().strip()
        if not url:self.status.setText('请先粘贴表格链接。');return
        saved_map=self.saved_map;self._invalidate();self.saved_map=saved_map
        def got(data):
            self.discovery=data;self.table.blockSignals(True);self.table.clear();self.table.addItem('请选择数据表','')
            for t in data['tables']:self.table.addItem(t.get('name') or t.get('table_name') or '数据表',t['table_id'])
            index=self.table.findData(data['table_id'])
            if not data['table_id'] and len(data['tables'])==1:index=1
            self.table.setCurrentIndex(max(0,index));self.table.blockSignals(False);self._layout();self._load_fields()
        self.status.setText('正在读取表格…');self._run(lambda:saving.discover_tables(url),got)
    def _load_fields(self,*_):
        self.fields=[];self._layout()
        table=self.table.currentData()
        if not table or not self.discovery:return
        def got(fields):
            self.fields=fields
            saved=self.saved_map if (self.target.currentData() or {}).get('table_id')==table else {}
            choices,issues=mapping.suggest_mapping(fields,saved)
            self._mapping_widgets(choices)
            issues=[key for key in issues if key in self.map_widgets]
            self._set_mapping_expanded(bool(issues));self._layout();self.status.setText('表格已读取，请确认列对应后存入。' if not issues else '部分列无法确定，请展开调整列对应。')
        self._run(lambda:saving.table_fields(self.discovery['app_token'],table),got)
    def _clear_mapping(self):
        while self.grid.count():
            item=self.grid.takeAt(0)
            if item.widget():item.widget().deleteLater()
        self.map_widgets={}

    def _mapping_widgets(self,choices):
        self._clear_mapping()
        sources = [source for source in mapping.SOURCES
                   if self.context == 'manage' or any(
                       mapping.source_value(record, source.key) not in (None, '') for record in self.records)]
        for row,source in enumerate(sources):
            label=QLabel(source.name);self.grid.addWidget(label,row,0)
            combo=_Choice();combo.addItem('不存入这项',None)
            for f in self.fields:
                if mapping.compatible(source,f):combo.addItem(f"{f['field_name']} · {mapping.TYPE_NAMES.get(mapping.field_type(f),'其他')}",{'field_id':f['field_id'],'type':mapping.field_type(f)})
            # Creating a missing column is explicit and performed only after final Save.
            if self.context!='manage':combo.addItem('新增「'+source.name+'」列',{'create':source.key})
            val=choices.get(source.key)
            if val:combo.setCurrentIndex(max(0,combo.findData(val)))
            combo.currentIndexChanged.connect(self._mapping_summary)
            self.grid.addWidget(combo,row,1);self.map_widgets[source.key]=combo
        self._mapping_summary()
    def _mapping_summary(self,*_):
        selected=[(mapping.SOURCE_BY_KEY[k].name,w.currentText().split(' · ')[0]) for k,w in self.map_widgets.items() if w.currentData()]
        skipped=[source.name for source in mapping.SOURCES
                 if self.map_widgets.get(source.key) and not self.map_widgets[source.key].currentData()
                 and any(mapping.source_value(record,source.key) not in (None,'') for record in self.records)]
        text=f'已对应 {len(selected)} 项信息。'
        if skipped:text+='本次不会存入：'+'、'.join(skipped)+'。'
        elif not self.records:text+='未对应的项目会跳过。'
        self.mapping_summary.setText(text)
        self.mapping_summary.setVisible(bool(skipped) or not self.records)
        self.mapping_title.setText(f'列对应 · {len(selected)} 项' if selected else '确认列对应')

    def _selection(self):
        document=self.format.currentData()=='document';mode=self.mode.currentData()
        value={'format':'document' if document else 'table','mode':mode,'operation_id':self.operation_id,'repeat_mode':self.repeat.currentData(),'account_id':self.account['account_id'],'name':self.name.text().strip(),'context':self.context,'single_document':len(self.records)==1,'template_id':self.template.currentData(),'remember':True}
        if not document and mode=='existing':
            if not self.fields or not self.table.currentData():raise ValueError('请先读取并选择目标数据表。')
            chosen={k:w.currentData() for k,w in self.map_widgets.items()}
            existing={k:v for k,v in chosen.items() if not v or not v.get('create')}
            additions=[k for k,v in chosen.items() if v and v.get('create')]
            prospective=list(self.fields);prospective_map=dict(existing)
            for key in additions:
                source=mapping.SOURCE_BY_KEY[key]
                prospective.append({'field_id':'new:'+key,'field_name':source.name,'type':source.types[0]})
                prospective_map[key]={'field_id':'new:'+key,'type':source.types[0]}
            mapping.validate_mapping(prospective,prospective_map)
            if any(self.fields and f['field_name']==mapping.SOURCE_BY_KEY[k].name for k in additions for f in self.fields):raise ValueError('已有同名列，请选择其他列或跳过，不会自动修改原列类型。')
            value.update(app_token=self.discovery['app_token'],table_id=self.table.currentData(),url=saving.bitable_url_with_table(self.url.text(),self.table.currentData()),field_map=existing,add_fields=additions,name=self.table.currentText(),schema_fingerprint=mapping.schema_fingerprint(self.fields))
        else:
            if not value['name']:raise ValueError('请填写名称。')
            location=self.location.currentData() or {}
            if location.get('custom'):value['location_url']=self.location_url.text().strip()
            else:value.update(location or {'destination_kind':'drive','folder_token':''})
        return value
    def _save(self):
        if self.result and self.result.complete:self.accept();return
        try:selection=dict(self._last_selection) if self.result and self._last_selection else self._selection()
        except (ValueError,KeyError) as exc:
            self.status.setText(str(exc))
            if self.format.currentData()=='table' and self.mode.currentData()=='existing' and self.fields:
                self._set_mapping_expanded(True);self.scroll.ensureWidgetVisible(self.mapping_card)
            return
        self._cancel=False;self._last_selection=selection
        def call():
            if 'location_url' in selection:
                if not selection['location_url']:raise ValueError('请粘贴位置链接，或选择云文档默认位置。')
                selection.update(saving.resolve_location(selection.pop('location_url')))
            return saving.save_records(self.records,selection,cancelled=lambda:self._cancel,progress=lambda text:self._worker.progress.emit(text))
        self.status.setText('正在检查目标并保存…');self._run(call,self._saved)
    def _saved(self,result):
        self.result=result
        for widget in self.body.findChildren(QWidget):
            if isinstance(widget,(QPushButton,QLineEdit,QComboBox,QCheckBox)):widget.setEnabled(False)
        remembered=True
        if result.row_success and self._last_selection:
            remembered=self._remember_target(result.target)
        details=list(result.errors)
        if not remembered:details.append('已存入飞书，但未能记住本次位置，下次需重新选择。')
        if len(result.urls)>1:details.extend(result.urls)
        self.result_details.setPlainText('\n'.join(details));self.result_details.setVisible(bool(details))
        if details:self.scroll.ensureWidgetVisible(self.result_details)
        self.status.setText(f'已存入 {result.row_success}/{result.row_total} 项'+(f'，附件待补充 {result.attachment_failed} 项' if result.attachment_failed else '')+(f'，待核对 {result.row_unconfirmed} 项' if result.row_unconfirmed else '')+(f'，已停止 {result.row_cancelled} 项' if result.row_cancelled else ''))
        if not self.records:self.status.setText('作品库已创建。' if self.context=='create' else '已添加到常用库。')
        self.open_button.setVisible(bool(result.url));self.save_button.setText('完成' if result.complete else '重试未完成项');self.cancel_button.setText('关闭')
        if result.row_unconfirmed:
            self.save_button.setText('请先核对飞书');self.save_button.setEnabled(False)
        self.adjust_button.setVisible(not result.complete)
        self.cancel_button.setVisible(not result.complete)
        self.connect_button.setEnabled(False)
        self.edit_mapping.setEnabled(True)
        self._responsive()
        self.session.done.emit(True,result)
        if self.session.toast:self.session.toast(self.status.text())
    def _remember_target(self,target):
        # One last-used selection, never a managed list or a copy of cloud data.
        old=deepcopy(getattr(self.cfg,'feishu_last_save',{}) or {})
        account_id=self.account['account_id']
        value=deepcopy(old) if isinstance(old,dict) and old.get('account_id')==account_id else {}
        selection=self._last_selection
        value.update(account_id=account_id,format=selection.get('format','table'))
        if target and target.get('app_token') and target.get('table_id'):
            value['table']={k:deepcopy(target[k]) for k in ('name','url','app_token','table_id','field_map') if k in target}
        if selection.get('mode')=='new' or selection.get('format')=='document':
            location={k:selection[k] for k in ('destination_kind','folder_token','space_id','node_token') if k in selection}
            if location:
                location['name']=self.location.currentText() if not (self.location.currentData() or {}).get('custom') else '上次使用的位置'
                value['location']=location
        self.cfg.feishu_last_save=value
        try: saved=bool(self.cfg.save())
        except Exception: saved=False
        if not saved:self.cfg.feishu_last_save=old
        return saved

    def _adjust(self):
        self.result=None;self._last_selection=None;self.operation_id=str(uuid4())
        for widget in self.body.findChildren(QWidget):
            if isinstance(widget,(QPushButton,QLineEdit,QComboBox,QCheckBox)):widget.setEnabled(True)
        self.result_details.hide();self.adjust_button.hide();self.open_button.hide()
        self.save_button.setText('创建空库' if self.context=='create' else '添加到常用库' if self.context=='manage' else '存入飞书');self.cancel_button.setText('取消')
        self.cancel_button.show();self.connect_button.setEnabled(True);self._responsive()
        self.save_button.setEnabled(bool(self.account))
        self.status.setText('已保留填写内容，请调整后保存。选择另存一份会新建副本。')

    def _open_result(self):
        if self.result and self.result.url:QDesktopServices.openUrl(QUrl(self.result.url))
    def reject(self):
        if self.busy:
            self._cancel=True;self._closing=True;self.status.setText('正在停止；已发出的保存会先确认结果。');return
        self._connection_suspended=False
        super().reject()
    def closeEvent(self,event):
        if self.busy:self.reject();event.ignore()
        else:super().closeEvent(event)
