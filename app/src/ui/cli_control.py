"""Real-window execution of authenticated CLI management requests."""
from copy import deepcopy
import sys
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..core.agent_workspace.control import SETTINGS, validate_settings
from ..core.agent_workspace.documents import digest
from ..core.agent_workspace.store import encode


class _PollWorker(QThread):
    result = Signal(object, str)

    def __init__(self, nonce, reports, snapshot):
        super().__init__()
        self.nonce, self.reports, self.snapshot = nonce, reports, snapshot

    def request_cancel(self):
        self.requestInterruption()

    def run(self):
        try:
            from ..core.agent_workspace.client import dispatch
            result = dispatch('control_poll', nonce=self.nonce, reports=self.reports, snapshot=self.snapshot)
            self.result.emit(result, '')
        except Exception:
            self.result.emit(None, 'CLI 管理连接暂时不可用')


class CliControl(QObject):
    def __init__(self, window, *, enabled=None):
        super().__init__(window)
        self.window = window
        self.nonce = uuid4().hex
        self.reports, self.active = [], {}
        self.report_sequence = 0
        self.worker = None
        self.closing = False
        self.model_controller = None
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.poll)
        if enabled if enabled is not None else bool(getattr(sys, 'frozen', False)):
            self.timer.start()

    def snapshot(self):
        return dict(settings={key: getattr(self.window.cfg, key) for key in SETTINGS},
                    updates=self.window.update_controller.snapshot.to_dict())

    def _report(self, request_id, state, result=None, message=''):
        self.report_sequence += 1
        self.reports = [r for r in self.reports if r['request_id'] != request_id]
        self.reports.append(dict(request_id=request_id, state=state, result=result, message=message,
                                 _sequence=self.report_sequence))
        if state != 'running':
            self.active.pop(request_id, None)

    def poll(self):
        if self.closing or self.worker is not None:
            return
        self._track_updates()
        self._track_models()
        reports = deepcopy(self.reports[:50])
        worker = _PollWorker(self.nonce, [{k:v for k,v in r.items() if k != '_sequence'} for r in reports], self.snapshot())
        self.worker = worker
        def done(reply, error):
            if error or self.closing:
                return
            sent = {r['_sequence'] for r in reports}
            self.reports = [r for r in self.reports if r['_sequence'] not in sent]
            self.window._external_core_busy = reply.get('core_busy', False)
            self.window._external_core_models = set(reply.get('busy_models', []))
            for command in reply['actions']:
                try:
                    self.execute(command)
                except Exception as exc:
                    self._report(command['request_id'], 'failed', message=str(exc)[:2000])
        worker.result.connect(done)
        def settled():
            if self.worker is worker:
                self.worker = None
            worker.deleteLater()
        worker.finished.connect(settled)
        worker.start()

    def execute(self, command):
        request_id, domain, action, args = (command[k] for k in ('request_id', 'domain', 'action', 'arguments'))
        if command['session'] != self.nonce:
            raise ValueError('此操作不属于当前窗口，未执行')
        if domain == 'settings':
            validate_settings(args['changes'])
            current = {key: getattr(self.window.cfg, key) for key in SETTINGS}
            if digest(encode(current)) != args['expected_hash']:
                raise ValueError('窗口设置已有变化，请重新读取后再修改')
            previous = deepcopy(current)
            for key, value in args['changes'].items():
                setattr(self.window.cfg, key, value)
            try:
                if not self.window.cfg.save():
                    raise ValueError('设置未能保存')
            except Exception:
                for key, value in previous.items():
                    setattr(self.window.cfg, key, value)
                raise ValueError('设置未能保存，原设置保留') from None
            self.window._on_models_changed()
            updated = {key: getattr(self.window.cfg, key) for key in SETTINGS}
            self._report(request_id, 'succeeded', dict(settings=updated, settings_hash=digest(encode(updated))))
        elif domain == 'models':
            self._models(request_id, action, args)
        elif domain == 'updates':
            self._updates(request_id, action)
        else:
            raise ValueError('管理操作无效')

    def _models(self, request_id, action, args):
        from .model_settings.controller import MODEL_IDS
        page = self.window._ensure_page_loaded(1)
        controller = page.controller
        if self.model_controller is not controller:
            self.model_controller = controller
            controller.operation_settled.connect(self._model_settled)
        if action == 'migrate':
            key = 'storage'
            controller.storage_change_to(args['destination'])
        else:
            key = next((k for k, v in MODEL_IDS.items() if v == args['model_id']), None)
            if key is None:
                raise ValueError('模型编号无效')
            if args.get('feature'):
                key = 'qwen'
                controller.feature_action(args['feature'], 'install' if action == 'repair' else action,
                                          confirmed=True, local_archive=args.get('local_archive'))
                key = 'qwen'
            else:
                controller.local_action(key, 'install' if action == 'repair' else action,
                                        confirmed=True, local_archive=args.get('local_archive'))
        if key in controller.active or (key == 'qwen' and 'qwen-plan' in controller.active):
            self.active[request_id] = dict(domain='models', key=key, action=action, feature=args.get('feature'))
            self._report(request_id, 'running', dict(model_id=args['model_id'], action=action))
        else:
            self._report(request_id, 'succeeded', dict(model_id=args['model_id'], action=action))

    def _model_settled(self, key, error, paused):
        for request_id, value in list(self.active.items()):
            if value['domain'] == 'models' and value['key'] == key:
                if not error and value.get('feature') and value['action'] in {'install', 'repair', 'resume'}:
                    from ..core.synthesis import qwen3_deployment as dp
                    from .model_settings.controller import FEATURE_IDS
                    state = dp.read_state()['features'].get(FEATURE_IDS[value['feature']], {})
                    paused = paused or state.get('state') == 'paused'
                    if state.get('state') not in {'ready', 'paused'}:
                        error = state.get('error') or '此声音能力未就绪，请查询模型状态'
                self._report(request_id, 'failed' if error else 'paused' if paused else 'succeeded',
                    dict(action=value['action']), error)

    def _updates(self, request_id, action):
        controller = self.window.update_controller
        if controller.snapshot.state in {'installing', 'waiting_ready', 'recovering'}:
            raise ValueError('更新正在接管或恢复，请等待重开后核对结果')
        if action == 'cancel':
            controller.cancel()
            self._report(request_id, 'succeeded', controller.snapshot.to_dict(), '已请求停止，实际状态请继续查询')
            return
        if controller._worker is not None and controller._worker.isRunning():
            raise ValueError('已有更新操作正在进行，请查询进度或取消后重试')
        if action == 'check':
            controller.check(manual=True)
        elif action == 'download':
            controller.download()
        elif action in {'verify', 'stage'}:
            if not controller.snapshot.staged_app:
                raise ValueError('尚无已暂存的更新，请先检查并下载')
            controller.verify_staged()
        elif action == 'install':
            if controller.launch_install() != 'launching':
                raise ValueError('当前有活动任务、未保存内容或安装准备不足，未重启 Lulu')
        self.active[request_id] = dict(domain='updates', action=action)
        self._report(request_id, 'running', controller.snapshot.to_dict())

    def _track_updates(self):
        controller = self.window.update_controller
        for request_id, value in list(self.active.items()):
            if value['domain'] != 'updates':
                continue
            snapshot = controller.snapshot.to_dict()
            if controller._worker is not None:
                self._report(request_id, 'running', snapshot)
                continue
            if value['action'] == 'install' and snapshot['state'] in {'installing', 'waiting_ready', 'recovering'}:
                self._report(request_id, 'running', snapshot, '更新接管中，重开后请核对实际版本与更新状态')
            else:
                state, ui = snapshot['state'], snapshot['ui_state']
                if value['action'] == 'download' and state == 'download_paused':
                    outcome = 'paused'
                elif snapshot['error_code'] or state.startswith('failed') or ui in {
                        'manifest_verify_error', 'verify_error', 'zip_structure_error',
                        'preparation_error', 'download_error', 'check_error', 'rollback_failed'}:
                    outcome = 'failed'
                elif (state in {'checking', 'downloading', 'downloaded'} or
                      (value['action'] in {'download', 'verify', 'stage'} and
                       (state != 'ready_to_install' or not snapshot['staged_app']))):
                    outcome = 'needs_input'
                else:
                    outcome = 'succeeded'
                message = snapshot['message'] or ('更新尚未取得完成结果，请核对状态后重试' if outcome == 'needs_input' else '')
                self._report(request_id, outcome, snapshot, message)

    def _track_models(self):
        if self.model_controller is None:
            return
        for request_id, value in list(self.active.items()):
            if value['domain'] == 'models':
                job = self.model_controller.jobs.get(value['key'], {})
                self._report(request_id, 'running', dict(action=value['action'], **job))

    def shutdown(self, timeout_ms=0):
        self.closing = True
        self.timer.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_cancel()
            if timeout_ms:
                self.worker.wait(timeout_ms)
            return not self.worker.isRunning()
        return True
