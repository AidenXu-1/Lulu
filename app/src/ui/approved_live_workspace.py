"""The approved v4 live workspace backed by Lulu's existing recording service."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from ..core.live_recording.contracts import LiveError, public_link, recording_message
from .approved_workspaces import ApprovedWorkspaceView
from .indextts2_settings import reduced_motion
from .live_recording_page import LiveRecordingPage, duration


_VIEW_FILTERS = {
    'hosts': {'all', 'live', 'offline', 'unknown'},
    'recordings': {'all', 'today', 'week', 'month'},
}


def _record(row):
    state = recording_message(row)
    return dict(
        id=row['id'], name=row['name'] or '待识别主播', started=row['started'] * 1000,
        seconds=int(row['duration']), bytes=row.get('bytes', 0), segmentCount=row.get('n', 0),
        status=state, clockRunning=bool(row.get('clock_running')),
        interrupted=row['ended'] is not None and state != '已保存',
        blocked=row.get('delete_blocked', '') or ('录制结束后可移入废纸篓' if row['ended'] is None else ''),
    )


def _host(row):
    status = row.get('live_state', 'unknown')
    return dict(
        id=row['id'], name=row['name'] or '待识别主播', url=row['source'].removeprefix('https://'),
        status=status if status in {'live', 'offline'} else 'unknown', checked=(row['checked'] or 0) * 1000,
        active=bool(row['enabled']), clockRunning=bool(row.get('clock_running')),
        seconds=int(row.get('duration') or 0), room=bool(row['live_url']),
        progress='\n'.join(filter(None, (row.get('progress_message'), row.get('comments_message')))),
        error=row.get('error_message', ''), needsLogin=row.get('live_error') == 'login_required',
    )


class ApprovedLiveWorkspace(LiveRecordingPage):
    """Keep native login, export monitoring and exit lifecycle on the existing page."""
    def __init__(self, controller, **kwargs):
        super().__init__(controller, **kwargs)
        native = QWidget(self)
        native.setLayout(self.layout())
        native.hide()
        self._view_params = dict(tab='hosts', query='', filter='all', page=1)
        self._details = None
        self._data_folder = None
        self._snapshot_session = ''
        self._snapshot_tab = 'hosts'
        self._ready_session = ''
        self._show_storage_on_load = False
        self._motion = reduced_motion()
        self.approved = ApprovedWorkspaceView(
            self, 'live',
            ('base.css', 'workspace.css', 'live-data.css', 'embedded.css', 'live.js'),
            ('select-controls.css', 'select-controls.js', 'channel.js', 'paper.png'),
            self._opened,
            {
                'view': self._view, 'add': self._request_add, 'start': self._request_start,
                'pause': self._request_pause, 'remove': self._request_remove,
                'pause_all': self._request_pause_all, 'delete': self._request_delete,
                'comments_setting': self._request_comments_setting,
                'open_room': self._request_room, 'login': self._request_login, 'clear_login': self._request_clear_login,
                'directory': self._request_directory, 'open_folder': self._request_open_folder,
                'detail': self._request_detail, 'data': self._request_data, 'comments': self._request_comments,
                'open_data_folder': self._request_open_data_folder,
                'open_record_folder': self._request_open_record_folder,
                'export_recording': self._request_export, 'transcribe': self._request_transcribe, 'comments_export': self._request_comments_export,
                'copy': self._request_copy, 'cancel_exports': self._request_cancel_exports,
            },
        )
        self.approved.setAccessibleName('直播录制')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.approved)

    def _opened(self):
        self.approved.bridge.send('motion', self._motion)
        if self.data:
            self.render(self.data)
        if self._show_storage_on_load:
            self.show_storage_location()

    def show_storage_location(self):
        self._show_storage_on_load = not self.approved.bridge.is_ready
        if not self._show_storage_on_load:
            self.approved.bridge.send('storage',True)

    def refresh(self):
        if hasattr(self, 'approved') and self.isVisible() and self.approved.bridge.is_ready:
            self.controller.view(**self._view_params)

    def render(self, data):
        self.data = data
        self._snapshot_session = self.controller.session
        self._snapshot_tab = self._view_params['tab']
        self._credential_state(bool(data['credential']))
        attention = set(data.get('attention_keys', ()))
        if self._attention_keys is not None and attention - self._attention_keys:
            self._needs_attention()
        self._attention_keys = attention
        if not hasattr(self, 'approved'):
            return
        if self._ready_session != self._snapshot_session:
            self._details = None
            self._data_folder = None
            self._ready_session = self._snapshot_session
        tab = self._view_params['tab']
        formatter = _host if tab == 'hosts' else _record
        label, saved = self.login_state()
        self.approved.bridge.send('snapshot', dict(
            tab=tab, hosts=data['hosts'], recordings=data['recordings'],
            monitoring=data['monitoring'], recording=data['recording'],
            free=data['free_bytes'] / (1024 ** 3) if data['free_bytes'] is not None else None,
            diskWarning=bool(data['disk_warning']), folder=data['directory'],
            comments=bool(data['comments_enabled']), connected=saved, loginLabel=label,
            rows=[formatter(row) for row in data['rows']],
            page=data['page'], pages=data['pages'], total=data['total'],
        ))

    def _view(self, done, *, tab, query, filter, page):
        if (tab not in _VIEW_FILTERS or type(query) is not str or len(query) > 300
                or filter not in _VIEW_FILTERS[tab] or type(page) is not int or not 1 <= page <= 99999):
            raise ValueError('列表参数无效')
        self._view_params = dict(tab=tab, query=query, filter=filter, page=page)
        self.refresh()
        done()

    def _message(self, text):
        if hasattr(self, 'approved') and text:
            self.approved.bridge.send('notice', dict(message=text, error=False))

    def _error(self, text):
        if self.credentials.isVisible():
            self.credential_status.setText(text)
        if hasattr(self, 'approved'):
            self.approved.bridge.send('notice', dict(message=text, error=True))

    def _row(self, tab, id):
        if self._snapshot_session == self.controller.session and self._snapshot_tab == tab == self._view_params['tab']:
            return next((row for row in self.data.get('rows', ()) if row['id'] == id), None)
        return None

    def _call(self, done, operation, transform=None, **arguments):
        # A dispatched write may finish after the view changes. Reply only to its
        # own request; the recording controller owns the lease and task lifetime.
        def success(value):
            try:
                done(transform(value) if transform else value)
            except (ValueError, KeyError, TypeError, RuntimeError):
                done(message='结果未能显示，请刷新后重试')
        self.controller.call(operation, callback=success,
                             failure=lambda: done(message='操作未完成，请查看提示后重试'), **arguments)

    def _request_add(self, done, *, source):
        if type(source) is not str or not 0 < len(source.strip()) <= 4096:
            return done(message='请粘贴有效的抖音主页或直播间链接。')
        try:
            normalized = public_link(source)
        except (LiveError, ValueError):
            return done(message='请粘贴有效的抖音主页或直播间链接。')
        self._call(done, 'add', lambda reply: dict(alreadyPresent=bool(reply.get('already_present'))),
                   source=normalized)

    def _host_action(self, done, operation, id):
        if not self._row('hosts', id):
            return done(message='这位主播已不在当前列表，请刷新后重试')
        self._call(done, operation, lambda _: {}, host_id=id)

    def _request_start(self, done, *, id):
        if self.controller.login_worker or self._credential_pending:
            return done(message='请先完成或取消抖音登录，再开始监控')
        self._host_action(done, 'start', id)

    def _request_pause(self, done, *, id):
        self._host_action(done, 'pause', id)

    def _request_remove(self, done, *, id):
        self._host_action(done, 'remove', id)

    def _request_pause_all(self, done):
        self._call(done, 'pause_all', lambda _: {})

    def _request_delete(self, done, *, id):
        row = self._row('recordings', id)
        if row is None or row['ended'] is None or row.get('delete_blocked'):
            return done(message='这场录像当前不能移入废纸篓')
        self._call(done, 'delete_recording',
                   lambda reply: dict(message='本场录像、直播数据和弹幕记录已移入系统废纸篓'
                                      if reply['trashed'] else '已移除失效录像记录，未改动文件'),
                   recording_id=id)

    def _request_comments_setting(self, done, *, enabled):
        if type(enabled) is not bool:
            raise ValueError('开关参数无效')
        self._call(done, 'comments_setting', lambda reply: dict(enabled=bool(reply['enabled'])), enabled=enabled)

    def _request_room(self, done, *, id):
        row = self._row('hosts', id)
        if row is None or not row['live_url']:
            return done(message='直播间地址尚未确认')
        url = QUrl(row['live_url'])
        if url.scheme() != 'https' or url.host() != 'live.douyin.com':
            return done(message='直播间地址不可用')
        if QDesktopServices.openUrl(url):
            done()
        else:
            done(message='无法打开直播间')

    def _request_login(self, done):
        self._settings('login')
        done()

    def _request_clear_login(self, done):
        self._settings('clear')
        done()

    def _request_directory(self, done):
        chosen = QFileDialog.getExistingDirectory(self, '选择录像保存位置', self.data.get('directory', ''))
        if not chosen:
            return done(dict(changed=False))
        self.approved.bridge.send('directory_pending', True)
        self._call(done, 'directory', lambda _: dict(changed=True), path=chosen)

    @staticmethod
    def _open_local_folder(done, path):
        if path and QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            done()
        else:
            done(message='无法打开文件夹，请检查保存位置')

    def _request_open_folder(self, done):
        self._open_local_folder(done, self.data.get('directory'))

    def _request_detail(self, done, *, id):
        if not self._row('recordings', id):
            return done(message='录像记录不在当前列表')
        def project(reply):
            self._details = reply
            return dict(segments=[
                dict(ready=bool(segment['closed'] and segment['valid']),
                     seconds=int(segment['duration'] or 0),
                     state='录制中' if not segment['closed'] and reply['ended'] is None else '无法使用')
                for segment in reply['segments']
            ])
        self._call(done, 'detail', project, recording_id=id)

    def _request_data(self, done, *, id):
        if not self._row('recordings', id):
            return done(message='录像记录不在当前列表')
        def project(reply):
            self._data_folder = id, reply.get('folder', '')
            return dict(
                samples=reply['samples'], sampleCount=reply['sample_count'], seconds=int(reply['duration']),
                peak=reply['peak_viewers'], latest=reply['last_viewers'], status=recording_message(reply),
                clockRunning=bool(reply.get('clock_running')), hasFolder=bool(self._data_folder[1]),
                dataError=reply['data_error'],
            )
        self._call(done, 'data', project, recording_id=id)

    def _request_open_data_folder(self, done, *, id):
        folder = self._data_folder
        if not folder or folder[0] != id or self._ready_session != self.controller.session:
            return done(message='直播数据位置还未确认')
        self._open_local_folder(done, folder[1])

    def _record_details(self, id):
        details = self._details
        if details and details['id'] == id and self._ready_session == self.controller.session:
            return details
        raise ValueError('录像记录需要重新读取')

    def _request_open_record_folder(self, done, *, id):
        self._open_local_folder(done, self._record_details(id)['directory'])

    def _segment(self, details, index):
        if type(index) is not int or not 0 <= index < len(details['segments']):
            raise ValueError('分段无效')
        segment = details['segments'][index]
        if not segment['closed'] or not segment['valid']:
            raise ValueError('分段尚不可用')
        return segment

    def _request_transcribe(self, done, *, id, segment):
        row = self._segment(self._record_details(id), segment)
        self._call(done, 'transcribe', lambda reply: bool(self.transcribe(reply['path'])), segment_id=row['id'])

    def _request_export(self, done, *, id, segment, format):
        details = self._record_details(id)
        if format not in {'mp3', 'mp4'} or (segment is None and format != 'mp4'):
            raise ValueError('导出格式无效')
        name = (details['name'] or '直播录像').replace('/', '_').replace(':', '_')[:80]
        if segment is None:
            operation, arguments = 'export_recording', dict(recording_id=id)
            filename = name + '-整场'
        else:
            row = self._segment(details, segment)
            operation, arguments = 'export', dict(segment_id=row['id'], format=format)
            filename = name + '-' + Path(row['path']).stem
        suffix = '.' + format
        target, _ = QFileDialog.getSaveFileName(self, '导出录像', str(Path(details['directory']) / (filename + suffix)),
                                                f'{format.upper()} 文件 (*{suffix})')
        if not target:
            return done(dict(started=False))
        def started(reply):
            self.exporting[reply['export_id']] = format
            self._export_activity('正在导出 ' + format.upper())
            return dict(started=True)
        self._call(done, operation, started, path=str(Path(target).with_suffix(suffix)), **arguments)

    def _export_activity(self, message):
        self.approved.bridge.send('exports', dict(active=bool(self.exporting), message=message))

    def _export_done(self, eid, reply):
        super()._export_done(eid, reply)
        self._export_activity(self.export_progress.text() if self.exporting else '')

    def _request_cancel_exports(self, done):
        self._cancel_exports()
        self._export_activity(self.export_progress.text())
        done()

    def _request_comments(self, done, *, id, query, page):
        if (not self._data_folder or self._data_folder[0] != id or self._ready_session != self.controller.session
                or type(query) is not str or len(query) > 300 or type(page) is not int or not 1 <= page <= 99999):
            raise ValueError('弹幕读取参数无效')
        def project(reply):
            rows = []
            for index, row in enumerate(reply['messages']):
                stamp = row['received_at']
                elapsed = row.get('elapsed_seconds')
                key = row.get('row_key') or hashlib.sha256(json.dumps(
                    [id, query, reply['page'], index, row], ensure_ascii=False,
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                rows.append(dict(key=str(key),
                                 time=duration(int(elapsed)) if elapsed is not None else stamp,
                                 nickname=row['nickname'], text=row['text'],
                                 copy=f"[{stamp}] {row['nickname']}：{row['text']}"))
            return dict(messages=rows, total=reply['total'], matching=reply['matching'], page=reply['page'],
                        pages=reply['pages'], state=reply['comments_state'], readError=reply['read_error'])
        self._call(done, 'comments', project, recording_id=id, query=query, page=page)

    def _request_comments_export(self, done, *, id, format):
        if (format not in {'txt', 'csv'} or not self._data_folder or self._data_folder[0] != id
                or self._ready_session != self.controller.session):
            raise ValueError('导出参数无效')
        folder = self._data_folder[1]
        suggested = str(Path(folder) / ('直播弹幕.' + format)) if folder else '直播弹幕.' + format
        target, _ = QFileDialog.getSaveFileName(self, '导出本场弹幕', suggested, f'{format.upper()} 文件 (*.{format})')
        if not target:
            return done(dict(saved=False))
        if not target.lower().endswith('.' + format):
            target += '.' + format
        self._call(done, 'comments_export', lambda _: dict(saved=True), recording_id=id, format=format, path=target)

    def _request_copy(self, done, *, text):
        if type(text) is not str or len(text) > 40000:
            raise ValueError('复制内容超出范围')
        QGuiApplication.clipboard().setText(text)
        done(True)
