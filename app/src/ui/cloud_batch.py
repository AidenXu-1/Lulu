"""Explicit cloud batch intake; the same account-aware service is used by the CLI."""
from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QFileDialog, QMessageBox, QComboBox, QGroupBox, QFormLayout, QLineEdit, QWidget,
    QScrollArea, QFrame)


class RequestWorker(QThread):
    result = Signal(object, str)

    def __init__(self, operation, arguments):
        super().__init__()
        self.operation, self.arguments = operation, arguments

    def request_cancel(self):
        self.requestInterruption()

    def run(self):
        try:
            from ..core.agent_workspace.client import dispatch
            self.result.emit(dispatch(self.operation, **self.arguments), '')
        except Exception:
            self.result.emit(None, '操作未获确认。请刷新批次核对，未自动重发；密钥请在安全设置中配置。')


class CloudBatchDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle('云端批量转录')
        self.resize(780, 700)
        self.main, self.worker, self.files = main, None, []
        self.closing = False
        self.after_settled = None
        self.submitting = False
        self.pending_commands = []
        self.pending_read = None
        self.settling = False
        self.preferred_batch_id = None
        self.offset = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.scroller.setWidget(content)
        outer.addWidget(self.scroller)
        from ..core.engines.providers import PRESETS
        self.provider = QComboBox()
        self.provider.setAccessibleName('云端转录服务')
        for preset in PRESETS.values():
            if preset.requires_network and not preset.retired:
                self.provider.addItem(preset.display_name,preset.id)
        current = self.provider.findData(getattr(getattr(main,'cfg',None),'engine_name','dashscope'))
        if current >= 0:
            self.provider.setCurrentIndex(current)
        layout.addWidget(self.provider)
        self.note = QLabel('音频会上传至所选服务并按账户计费，按服务额度和本机资源自动调度。\n取消先停止后续发送，在途结果完成后保存；结果未确认的音频不会自动重发。\n工作台多选云端音视频会进入此处，既有任务卡保持原流程。本地模型仍逐条处理。')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.provider_hint = QLabel()
        self.provider_hint.setWordWrap(True)
        layout.addWidget(self.provider_hint)
        self.provider.currentIndexChanged.connect(self.update_provider_hint)
        self.update_provider_hint()
        quota = QGroupBox('账号额度覆盖（可选，按服务商确认的额度填写）')
        quota.setCheckable(True)
        quota.setChecked(False)
        self.quota = quota
        quota_layout = QVBoxLayout(quota)
        quota_fields = QWidget()
        form = QFormLayout(quota_fields)
        quota_layout.addWidget(quota_fields)
        quota_fields.setVisible(False)
        quota.toggled.connect(quota_fields.setVisible)
        self.limit_fields = {}
        for key,label in [('requests_per_second','每秒请求数'),('requests_per_minute','每分钟请求数'),('requests_per_day','每天请求数'),('max_inflight','同时处理数'),
                          ('audio_seconds_per_hour','每小时音频额度（秒）'),('audio_seconds_per_day','每天音频额度（秒）')]:
            edit = QLineEdit()
            edit.setPlaceholderText('留空自动调度')
            edit.setAccessibleName(label)
            self.limit_fields[key] = edit
            form.addRow(label,edit)
        layout.addWidget(quota)
        self.sources = QPlainTextEdit()
        self.sources.setPlaceholderText('每行一个作品或音视频链接；本地文件请点“添加文件”')
        self.sources.setMaximumHeight(90)
        layout.addWidget(self.sources)
        self.file_names = QPlainTextEdit()
        self.file_names.setReadOnly(True)
        self.file_names.setPlaceholderText('尚未选择本地文件')
        self.file_names.setMaximumHeight(65)
        layout.addWidget(self.file_names)
        row = QHBoxLayout()
        self.add = QPushButton('添加文件')
        self.add.clicked.connect(self.pick)
        self.start = QPushButton('上传并开始')
        self.start.clicked.connect(self.create)
        row.addWidget(self.add)
        row.addWidget(self.start)
        clear = QPushButton('清空选择')
        self.clear_button = clear
        clear.clicked.connect(self.clear_sources)
        row.addWidget(clear)
        layout.addLayout(row)
        self.selection = QComboBox()
        self.selection.currentIndexChanged.connect(self.show_selected)
        layout.addWidget(self.selection)
        for actions in [[('刷新批次', self.refresh), ('取消本批', self.cancel), ('恢复未发送项', self.resume)],
                        [('重试未确认项', self.retry_unknown), ('强制结束',self.force)]]:
            row = QHBoxLayout()
            for title, action in actions:
                button = QPushButton(title)
                button.clicked.connect(action)
                row.addWidget(button)
                if title == '恢复未发送项':
                    self.resume_button = button
            layout.addLayout(row)
        page = QHBoxLayout()
        for label,step in [('上一页',-100),('下一页',100)]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False,step=step:self.page(step))
            page.addWidget(button)
        self.page_label = QLabel('')
        page.addWidget(self.page_label)
        layout.addLayout(page)
        self.activity = QLabel('')
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        self.status = QPlainTextEdit()
        self.status.setReadOnly(True)
        layout.addWidget(self.status)
        completed = QHBoxLayout()
        self.completed = QComboBox()
        self.completed.setAccessibleName('选择本页完成文稿')
        completed.addWidget(self.completed, 1)
        open_completed = QPushButton('打开所选文稿')
        open_completed.clicked.connect(self.open_document)
        completed.addWidget(open_completed)
        layout.addLayout(completed)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.show_selected)
        self.timer.start()
        self.refresh()

    def request(self, operation, arguments, callback):
        if self.closing:
            return False
        if self.worker is not None or self.settling:
            request = (operation,arguments,callback)
            if operation in {'batch_prepare','batch_start','batch_cancel'}:
                for index,(old, args, _) in enumerate(self.pending_commands):
                    if operation == old and (arguments == args or operation == 'batch_cancel'
                            and arguments.get('batch_id') == args.get('batch_id')):
                        if not (old == 'batch_cancel' and args.get('force') and not arguments.get('force')):
                            self.pending_commands[index] = request
                        break
                else:
                    self.pending_commands.append(request)
                self.activity.setText('操作已收到，当前读取完成后优先执行；请勿重复提交。')
            else:
                self.pending_read = request
            return True
        worker = RequestWorker(operation, arguments)
        self.worker = worker
        def result(reply, error):
            if error:
                self.status.setPlainText(error)
                if operation == 'batch_prepare' or operation == 'batch_start' and not arguments.get('resume'):
                    self.set_submitting(False)
            else:
                callback(reply)
        worker.result.connect(result)
        def settled():
            self.worker = None
            worker.deleteLater()
            followup, self.after_settled = self.after_settled, None
            self.settling = True
            try:
                if followup and not self.closing:
                    followup()
            finally:
                self.settling = False
            if self.closing:
                return
            next_request = self.pending_commands.pop(0) if self.pending_commands else self.pending_read
            if next_request:
                if not self.pending_commands and next_request is self.pending_read:
                    self.pending_read = None
                self.request(*next_request)
            else:
                self.activity.clear()
        worker.finished.connect(settled)
        worker.start()
        return True

    def pick(self):
        files, _ = QFileDialog.getOpenFileNames(self, '添加音视频', '', '音视频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.aiff *.mp4 *.mov *.mkv *.webm)')
        self.files = list(dict.fromkeys([*self.files, *files]))
        self.add.setText(f'添加文件（已选 {len(self.files)}）')
        self.file_names.setPlainText('\n'.join(self.files))

    def update_provider_hint(self):
        self.provider_hint.setText({
            'volcengine':'本地音频直传按官方接口文档适配，尚未进行火山真实服务验收。使用豆包语音 API Key，需开通极速版。准备后的 WAV 需在 100MB、2小时以内。',
            'custom_openai':'使用安全设置中保存的接口和模型。未知额度会从小规模逐步增加，可按服务商规则填写覆盖值。',
        }.get(self.provider.currentData(),'按具体模型和账号额度平滑发送，遇到限流会自动退让。'))

    def clear_sources(self):
        self.files = []
        self.sources.clear()
        self.file_names.clear()
        self.add.setText('添加文件')

    def set_submitting(self, submitting):
        self.submitting = submitting
        for widget in (self.start,self.add,self.clear_button,self.sources,self.provider,self.quota):
            widget.setEnabled(not submitting)

    def consent(self, provider=None):
        from ..core.engines.providers import PRESETS
        pid = provider or self.provider.currentData()
        if PRESETS[pid].retired:
            if provider and (getattr(self,'batch',None) or {}).get('local_recovery_count',0):
                self.activity.setText('仅补保存本机已有结果，不会再次上传。')
                return True
            self.status.setPlainText('此云端转录服务已移除，历史文稿仍可打开；请选择其他服务建立新批次。')
            return False
        name = PRESETS[pid].display_name
        count = len(self.files)+len([s for s in self.sources.toPlainText().splitlines() if s.strip()]) if provider is None else None
        quantity = f'本次选择共 {count} 条。' if count is not None else ''
        return QMessageBox.question(self, '确认上传与计费', f'{quantity}本批音频将上传至{name}并按你的账户计费，按服务额度自动调度。明确限流拒绝后可稍后重试，结果未确认时不会自动重传。取消或强制结束后已发送部分可能仍计费。是否开始？', QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def create(self):
        if self.submitting:
            return
        from ..core.audio.extractor import extract_url
        links = [line.strip() for line in self.sources.toPlainText().splitlines() if line.strip()]
        if any(not extract_url(line) for line in links) or not self.files and not links:
            self.status.setPlainText('请添加音视频文件，或每行填写一个有效链接。')
            return
        limits = {}
        try:
            if self.quota.isChecked():
                for key,edit in self.limit_fields.items():
                    if edit.text().strip():
                        limits[key] = int(edit.text()) if key == 'max_inflight' else float(edit.text())
                from ..core.engines.cloud_limits import policy_for
                policy_for(self.provider.currentData(),overrides=limits)
        except (ValueError,TypeError):
            self.status.setPlainText('账号额度请填写服务商确认的正数，或留空使用自动调度。')
            return
        if not self.consent():
            return
        self.set_submitting(True)
        self.submitted_sources = (list(self.files),self.sources.toPlainText().splitlines())
        def prepared(plan):
            self.after_settled = lambda: self.begin(plan)
        self.request('batch_prepare', dict(request_id='cloud-' + uuid4().hex,
            items=[dict(source=source) for source in [*self.files, *links]], engine=self.provider.currentData(),
            cloud_concurrency=None,cloud_limits=limits or None), prepared)

    def begin(self, plan, resume=False,retry_unknown=False):
        def started(batch):
            if self.submitting and not resume:
                files,links = self.submitted_sources
                incoming_files = [f for f in self.files if f not in files]
                incoming_links = [s for s in self.sources.toPlainText().splitlines() if s not in links]
                self.clear_sources()
                self.files = incoming_files
                self.file_names.setPlainText('\n'.join(self.files))
                self.sources.setPlainText('\n'.join(incoming_links))
                if self.files:
                    self.add.setText(f'添加文件（已选 {len(self.files)}）')
                self.set_submitting(False)
            if not resume:
                # Selection and displayed data change together, before a list
                # refresh can be delayed. Cancel must target the visible batch.
                bid = batch['batch_id']
                self.selection.blockSignals(True)
                index = self.selection.findData(bid)
                if index < 0:
                    self.selection.addItem(f"新批次 · 共{batch['total']}条",bid)
                    index = self.selection.count()-1
                self.selection.setCurrentIndex(index)
                self.selection.blockSignals(False)
                self.offset = 0
                self.last_bid = bid
                self.preferred_batch_id = bid
            if self.selection.currentData() == batch['batch_id']:
                self.render(batch)
            self.after_settled = self.refresh
        self.request('batch_start', dict(batch_id=plan['batch_id'], expected_hash=plan['plan_hash'],
            network_consent=True, cloud_upload_consent=True, resume=resume,retry_unknown=retry_unknown), started)

    def refresh(self):
        def listed(reply):
            chosen = self.preferred_batch_id or self.selection.currentData()
            self.selection.blockSignals(True)
            self.selection.clear()
            for batch in reply['batches']:
                from ..core.engines.cloud_requests import cloud_ids
                from ..core.engines.providers import PRESETS
                if batch.get('engine') in cloud_ids() and not batch.get('workspace'):
                    label = PRESETS[batch['engine']].display_name + (' · 已移除' if PRESETS[batch['engine']].retired else '')
                    self.selection.addItem(f"{label} · 共{batch['total']}条 · 完成{batch['counts']['succeeded']}条", batch['batch_id'])
            index = self.selection.findData(chosen)
            if index >= 0:
                self.selection.setCurrentIndex(index)
                self.preferred_batch_id = None
            self.selection.blockSignals(False)
            self.clear_stale_view(self.selection.currentData())
            self.after_settled = self.show_selected
        self.request('batch_list', {}, listed)

    def show_selected(self, *_):
        bid = self.selection.currentData()
        self.clear_stale_view(bid)
        if bid:
            if self.preferred_batch_id and bid != self.preferred_batch_id:
                self.preferred_batch_id = None
            if getattr(self,'last_bid',None) != bid:
                self.offset = 0
                self.last_bid = bid
            offset = self.offset
            self.request('batch_show', dict(batch_id=bid,offset=offset,limit=100),
                         lambda batch:self.render(batch) if self.selection.currentData() == bid and self.offset == offset else None)

    def clear_stale_view(self, bid):
        if (getattr(self,'batch',None) or {}).get('batch_id') != bid:
            self.batch = None
            self.completed.clear()
            self.completed.addItem('等待读取所选批次',None)
            self.page_label.clear()
            self.status.setPlainText('正在读取所选批次…' if bid else '暂无云端批次')

    def page(self, step):
        batch = getattr(self,'batch',None) or {}
        if step > 0 and not batch.get('has_more'):
            return
        self.offset = max(0,self.offset+step)
        self.show_selected()

    def render(self, batch):
        if self.selection.currentData() != batch['batch_id']:
            return
        self.batch = batch
        from ..core.engines.providers import PRESETS
        self.resume_button.setText('补保存已有结果' if PRESETS[batch['engine']].retired else '恢复未发送项')
        self.selection.setItemText(self.selection.currentIndex(),
            f"{PRESETS[batch['engine']].display_name}{' · 已移除' if PRESETS[batch['engine']].retired else ''} · 共{batch['total']}条 · 完成{batch['counts']['succeeded']}条")
        states = dict(prepared='待开始', running='处理中', succeeded='完成', partial='部分完成', failed='失败', cancelled='已取消', interrupted='已中断', duplicate='重复来源，沿用原项', recovery_required='结果待核对', pending='等待中')
        lines = [f"{states.get(batch['state'], batch['state'])} · {batch['progress']}% · 共 {batch['total']} 条"]
        if batch.get('message'):
            lines.append(batch['message'])
        for item in batch['items']:
            lines.extend([f"{item['index']+1}. {item['title']} · {states.get(item['state'], item['state'])}", item.get('message', '')])
        self.status.setPlainText('\n'.join(lines))
        selected = self.completed.currentData()
        self.completed.clear()
        for item in batch['items']:
            if item.get('document_id'):
                self.completed.addItem(f"{item['index']+1}. {item['title']}",item['document_id'])
        index = self.completed.findData(selected)
        if index >= 0:
            self.completed.setCurrentIndex(index)
        if not self.completed.count():
            self.completed.addItem('本页暂无完成文稿',None)
        self.page_label.setText(f"第 {batch.get('offset',0)+1 if batch['items'] else 0} 至 {batch.get('offset',0)+len(batch['items'])} 条 / 共 {batch['total']} 条")

    def cancel(self):
        bid = self.selection.currentData()
        if bid:
            self.request('batch_cancel', dict(batch_id=bid),
                         lambda batch:self.render(batch) if self.selection.currentData() == bid else None)

    def resume(self):
        batch = getattr(self, 'batch', None)
        if batch and batch['batch_id'] == self.selection.currentData() and batch['state'] not in {'running', 'succeeded'} and self.consent(batch['engine']):
            self.begin(batch, resume=True)

    def retry_unknown(self):
        batch = getattr(self,'batch',None) or {}
        if batch.get('batch_id') != self.selection.currentData():
            self.activity.setText('正在读取所选批次，请稍后再确认重试。')
            return
        from ..core.engines.providers import PRESETS
        if getattr(PRESETS.get(batch.get('engine')), 'retired', False):
            self.activity.setText('此云端转录服务已移除，不再上传；历史文稿仍可打开。')
            return
        count = batch.get('unconfirmed_count',0)
        if not count or batch.get('state') == 'running':
            return
        if QMessageBox.question(self,'可能重复计费',f'有{count}条音频的上次请求结果未确认，服务端可能已经处理并计费。再次上传可能重复收费。是否明确重试这些音频？',QMessageBox.Yes|QMessageBox.No,QMessageBox.No) == QMessageBox.Yes:
            if self.consent(batch['engine']):
                self.begin(batch,resume=True,retry_unknown=True)

    def force(self):
        bid = self.selection.currentData()
        if bid and QMessageBox.question(self,'强制结束本机请求',
                '将停止本机在途请求，可能收不到已付费结果。服务端可能继续处理和计费，未确认项不会自动重发。是否强制结束？',
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No) == QMessageBox.Yes:
            self.request('batch_cancel',dict(batch_id=bid,force=True),
                         lambda batch:self.render(batch) if self.selection.currentData() == bid else None)

    def open_document(self):
        if (getattr(self,'batch',None) or {}).get('batch_id') != self.selection.currentData():
            return
        eid = self.completed.currentData()
        if eid:
            self.main.open_material_entry(eid, 'transcript')

    def closeEvent(self, event):
        # Closing this view does not cancel the durable batch or destroy a live QThread.
        self.timer.stop()
        if self.worker is not None:
            self.hide()
            event.ignore()
            return
        event.accept()

    def shutdown(self):
        self.closing = True
        self.after_settled = None
        self.pending_commands.clear()
        self.pending_read = None
        self.timer.stop()
