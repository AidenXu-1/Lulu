"""GUI-owned settings/model/update actions, with measured GUI acknowledgements.

The real window polls this queue. CLI requests cannot impersonate the window,
extend its life, or report their own successful execution.
"""
from copy import deepcopy
import threading
import time

from .store import WorkspaceStore, identity, encode
from .documents import digest
from .. import library

SETTINGS = {
    'engine_name': str, 'enable_link_download': bool, 'prefer_subtitles': bool,
    'keep_media': bool, 'link_media_mode': str, 'media_dir': str, 'last_output_dir': str,
    'obsidian_dir': str, 'auto_update_enabled': bool, 'notification_policy': str,
    'prevent_sleep_during_tasks': bool, 'confirm_exit_during_tasks': bool,
    'completion_action': str, 'notify_on_failure': bool, 'notification_sound_enabled': bool,
    'default_tts_model': str,
}


def validate_settings(changes):
    if type(changes) is not dict or not changes or set(changes) - SETTINGS.keys():
        raise ValueError('只接受已列出的非敏感设置；密钥、Cookie、连接地址请在安全设置处理')
    for key, value in changes.items():
        if type(value) is not SETTINGS[key]:
            raise ValueError('设置类型无效：' + key)
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError('设置内容过长')
    from ..engines.local_models import LOCAL_MODELS
    from ..synthesis.cli_options import MODELS
    enums = dict(engine_name=LOCAL_MODELS, default_tts_model=MODELS, link_media_mode={'audio', 'video'},
                 notification_policy={'always', 'inactive', 'never'}, completion_action={'stay', 'open_result'})
    for key, options in enums.items():
        if key in changes and changes[key] not in options:
            raise ValueError('设置选项无效：' + key)
    from ..live_recording.index import directory
    for key in ('media_dir', 'last_output_dir', 'obsidian_dir'):
        if changes.get(key):
            directory(changes[key])


class ControlService:
    def __init__(self):
        self.store = WorkspaceStore()
        self.lock = threading.RLock()
        self.nonce, self.deadline = '', 0
        self.queue = []
        self.snapshot = {}

    def poll(self, nonce, reports, snapshot):
        identity(nonce)
        if type(reports) is not list or len(reports) > 50 or type(snapshot) is not dict:
            raise ValueError('窗口操作回执无效')
        with self.lock:
            if self.nonce != nonce:
                for request_id in self.queue:
                    row = self.store.preference('control:' + request_id)
                    if row and row['state'] in {'pending', 'running'}:
                        row.update(state='interrupted', message='原窗口已退出，未自动重做；请核对结果后提交新的操作编号')
                        self.store.preference('control:' + request_id, row)
                self.queue = []
            self.nonce, self.deadline = nonce, time.monotonic() + 15
            self.snapshot = deepcopy(snapshot)
            for report in reports:
                if type(report) is not dict or set(report) - {'request_id', 'state', 'result', 'message'}:
                    raise ValueError('窗口操作回执无效')
                request_id = identity(report.get('request_id'))
                row = self.store.preference('control:' + request_id)
                if not row or row['session'] != nonce or report.get('state') not in {
                    'running', 'succeeded', 'failed', 'cancelled', 'paused', 'needs_input'}:
                    raise ValueError('窗口操作回执不属于当前会话')
                if row['state'] in {'pending', 'running', 'interrupted'}:
                    row.update(state=report['state'], result=report.get('result'), message=str(report.get('message', ''))[:2000])
                    row['acknowledged'] = True
                    self.store.preference('control:' + request_id, row)
            actions = []
            for request_id in self.queue:
                row = self.store.preference('control:' + request_id)
                if row['state'] == 'pending':
                    actions.append(deepcopy(row))
                    # Claim before replying. A lost response requires explicit
                    # inspection, never another installation/destructive replay.
                    row['state'] = 'running'
                    row['claimed_at'] = time.time()
                    self.store.preference('control:' + request_id, row)
            return dict(actions=actions)

    def submit(self, request_id, domain, action, arguments):
        identity(request_id)
        intent = digest(encode([domain, action, arguments]))
        with self.lock:
            old = self.store.preference('control:' + request_id)
            if old:
                if old['intent'] != intent:
                    raise library.RevisionConflict('同一操作编号已有不同内容，未重复执行')
                return self.show(request_id)
            if time.monotonic() >= self.deadline:
                from ..transcription_job_application import LocalTranscriptionJobError
                raise LocalTranscriptionJobError('HUMAN_ACTION_REQUIRED', 'LULU_WINDOW_REQUIRED')
            self.queue = [request_id for request_id in self.queue if
                          self.store.preference('control:' + request_id)['state'] in {'pending', 'running'}]
            if len(self.queue) >= 20:
                raise ValueError('已有多项管理操作正在等待或执行，请先查询结果')
            row = dict(request_id=request_id, domain=domain, action=action, arguments=arguments,
                       session=self.nonce, intent=intent, state='pending', result=None, message='等待 Lulu 执行')
            if domain == 'updates' and action == 'install':
                row['result'] = {key: self.snapshot.get('updates', {}).get(key) for key in ('latest_version', 'latest_build')}
            self.store.preference('control:' + request_id, row)
            self.queue.append(request_id)
            return self.show(request_id)

    def show(self, request_id):
        row = self.store.preference('control:' + identity(request_id))
        if not row:
            raise FileNotFoundError(request_id)
        if (row['state'] == 'running' and not row.get('acknowledged')
                and time.time() - row.get('claimed_at', time.time()) > 30):
            row.update(state='interrupted', message='窗口未确认收到操作；请核对实际状态，未自动重发')
            self.store.preference('control:' + request_id, row)
        if row['state'] in {'pending', 'running', 'interrupted'} and row['domain'] == 'updates' and row['action'] == 'install':
            from ...version import __version__, __build__
            expected = row.get('result') or {}
            if (__version__, __build__) == (expected.get('latest_version'), expected.get('latest_build')):
                row.update(state='succeeded', result=dict(current_version=__version__, current_build=__build__,
                    verified_by='running_lulu_build'), message='重开后的 Lulu 版本与本次更新目标一致')
                self.store.preference('control:' + request_id, row)
        if row['state'] in {'pending', 'running'} and (row['session'] != self.nonce or time.monotonic() >= self.deadline):
            return dict(request_id=request_id, state='interrupted', result=row['result'],
                        message='窗口会话已失效，请核对实际状态；未自动重发')
        return {key: row[key] for key in ('request_id', 'state', 'result', 'message')}
