"""Core-owned monitors, leased producers and a bounded GUI media inventory."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import csv
import json
import os
from pathlib import Path
import random
import shutil
import threading
import time
from uuid import uuid4

from ..job_ledger.contracts import JobLedgerError, JobState, TERMINAL_STATES
from .contracts import (LiveError, LIVE_MESSAGES, MESSAGES, QUERY_ERRORS,
                        confirmed_live_state, public_link, recording_message, validate_request)
from .douyin import DouyinProvider
from .data import append_sample, data_path, read_data
from .index import LiveIndex, directory
from .media import SegmentProducer, bundled_ffmpeg, export_media, fingerprint, managed_file, probe

KIND = 'live_recording'
ACTIVE = {JobState.PREPARED, JobState.QUEUED, JobState.RUNNING, JobState.CANCEL_REQUESTED}
RECONNECT_TIMEOUT = 120.0  # No new video bytes; finish this recording, keep monitoring.

def uid(prefix):
    return prefix + '-' + uuid4().hex

def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def move_to_trash(path):
    # Qt uses the platform Trash API, including a recording's external volume.
    # Never fall back to permanent deletion or manually renaming into ~/.Trash.
    from PySide6.QtCore import QFile
    result = QFile.moveToTrash(str(path))
    # PySide may return bool on macOS despite its declared tuple signature.
    moved, target = result if isinstance(result, tuple) else (result, '')
    if moved is not True:
        raise LiveError('未能移入废纸篓，录像保留；请检查文件夹权限或磁盘连接')
    return target

class LiveService:
    def __init__(self, root, control, installation_id, *, provider=None, ffmpeg=None,
                 secret_read=None, secret_write=None, disk_free=None, producer_factory=SegmentProducer,
                 historical_recordings=None):
        self.index = LiveIndex(Path(root) / 'live-recording')
        self.control, self.installation_id = control, installation_id
        # Supplied only by the Core's verified handover, never by GUI arguments.
        self.historical_recordings = deepcopy(historical_recordings or {})
        self.provider = provider or DouyinProvider()
        self.ffmpeg = ffmpeg
        self.producer_factory = producer_factory
        self.secret_read, self.secret_write = secret_read, secret_write
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.lock = threading.RLock()
        # CSV I/O can be slow on external storage. Never acquire this while
        # holding the recording-control lock; pause/heartbeat must stay available.
        self.data_lock = threading.RLock()
        self.network_limit = threading.BoundedSemaphore(4)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='lulu-live-check')
        self.stop_event = threading.Event()
        self.session, self.nonce, self.deadline = '', '', 0.0
        self.closing = True
        self.credential = ''
        self.monitors, self.checks, self.exports = {}, {}, {}
        self.data_checks = set()
        self.deleting = {}
        self.transcribing = set()
        self.free, self.disk_warning, self.last_disk = None, False, 0.0
        self.recovery = []
        self.monitor_bytes, self.started_at = 0, time.monotonic()
        for row in self.index.all("SELECT * FROM recordings WHERE ended IS NULL OR reason='recovery_required'"):
            self.index.write('UPDATE recordings SET ended=COALESCE(ended,?), reason=? WHERE id=?',
                             (time.time(), 'recovery_required', row['id']))
            self.recovery.append(row)
        for row in self.index.all('SELECT DISTINCT r.* FROM recordings r JOIN segments s ON r.id=s.recording_id WHERE s.valid=0'):
            if all(old['id'] != row['id'] for old in self.recovery):
                self.recovery.append(row)
        # A Core restart never replays an interrupted producer or an old job.
        for job in self.control.list():
            if job.job_kind == KIND and job.state in ACTIVE:
                if job.state is JobState.CANCEL_REQUESTED:
                    self.control.settle_cancelled(job.job_id, expected_revision=job.job_revision,
                        changed_at=utc(), lease_token=self.control._store.current_lease_token(job.job_id)
                        if job.attempts[-1].worker_instance_id else None)
                else:
                    self.control._store.transition(job.job_id, expected_revision=job.job_revision,
                        new_state=JobState.FAILED,
                        changed_at=utc(), lease_token=self.control._store.current_lease_token(job.job_id)
                        if job.attempts[-1].worker_instance_id else None, reason_code='LIVE_CORE_RESTARTED')
        self.scheduler = threading.Thread(target=self._schedule, name='lulu-live-monitor', daemon=True)
        self.scheduler.start()

    def _current(self, token):
        if token != self.session or not token or (not self.closing and time.monotonic() >= self.deadline):
            raise LiveError('直播会话已结束，请重新打开 Lulu')

    def _can_run(self, token):
        return token == self.session and not self.closing and time.monotonic() < self.deadline

    def _credentials(self):
        if not self.index.setting('credential', ''):
            return ''
        if self.secret_read is None:
            from ..secrets import get_key
            value = get_key('douyin-live', require_access=True)
        else:
            value = self.secret_read()
        if not value:
            raise LiveError('已配置的抖音凭据不存在，请重新添加或删除配置')
        return value

    def _read_credentials(self):
        try:
            self.credential = self._credentials()
        except Exception:
            raise LiveError('抖音凭据无法读取，请在本页重新添加；未开始监控') from None

    def handle(self, operation, arguments):
        validate_request(operation, arguments)
        if operation == 'data':
            return self._data(arguments)
        if operation == 'delete_recording':
            return self._delete_recording(arguments)
        if operation == 'transcribe':
            return self._transcribe(arguments)
        with self.lock:
            if operation == 'open':
                nonce = arguments['nonce']
                if self.session and not self.closing and time.monotonic() < self.deadline:
                    if nonce != self.nonce:
                        raise LiveError('已有 Lulu 窗口正在监控，请先退出原窗口')
                    return {'session': self.session}
                if self.busy():
                    raise LiveError('上一场录制正在收尾，请稍后重试')
                self.session, self.nonce = uid('session'), nonce
                self.deadline, self.closing = time.monotonic() + 30, False
                # A previous session's response is not a current observation.
                self.index.write("UPDATE hosts SET live_state='unknown',live_error=''")
                enabled = self.index.all('SELECT * FROM hosts WHERE enabled=1')
                if enabled:
                    try:
                        self._read_credentials()
                    except LiveError:
                        for host in enabled:
                            self._status(host['id'], 'credential_error')
                        # Preserve intent; require explicit credential repair/start this session.
                        enabled = []
                for offset, host in enumerate(enabled):
                    self._enable(host, delay=offset * 1.5)
                if self.recovery:
                    rows, self.recovery = self.recovery, []
                    self._background_recovery(rows, self.session)
                return {'session': self.session}
            token = arguments['session']
            self._current(token)
            if operation == 'heartbeat':
                if not self.closing:
                    self.deadline = time.monotonic() + 30
                return {'active': self.busy(), 'stopped': self.closing and not self.busy()}
            if operation == 'stop':
                self._stop_session()
                return {'active': self.busy(), 'stopped': not self.busy()}
            if operation == 'snapshot':
                return self.snapshot(**{k: v for k, v in arguments.items() if k != 'session'})
            if self.closing:
                raise LiveError('Lulu 正在结束直播任务，请稍后操作')
            if operation == 'add':
                source = public_link(arguments['source'])
                old = self.index.one('SELECT * FROM hosts WHERE source=?', (source,))
                if old:
                    return {'host_id': old['id']}
                hid = uid('host')
                self.index.write('INSERT INTO hosts(id,source,created) VALUES(?,?,?)', (hid, source, time.time()))
                self._submit_check(hid, token, identify_only=True)
                return {'host_id': hid}
            if operation in {'start', 'pause', 'remove'}:
                host = self._host(arguments['host_id'])
                if operation == 'start':
                    if self.monitors.get(host['id'], {}).get('thread'):
                        raise LiveError('正在保存最后一段，请稍后开始')
                    if not self.ffmpeg:
                        self.ffmpeg = bundled_ffmpeg()
                    output = directory(self.output_dir(), create=True)
                    if self.disk_free(output) < 2 * 1024**3:
                        raise LiveError('可用空间不足 2 GB，请先释放空间或更换保存位置')
                    self._read_credentials()
                    self.index.write('UPDATE hosts SET enabled=1 WHERE id=?', (host['id'],))
                    self._enable(host)
                else:
                    self._pause(host['id'])
                    if operation == 'remove':
                        monitor = self.monitors.get(host['id'])
                        if monitor and monitor.get('thread'):
                            monitor['remove'] = True
                        else:
                            self.index.write('DELETE FROM hosts WHERE id=?', (host['id'],))
                            self.monitors.pop(host['id'], None)
                return {'changed': True}
            if operation == 'pause_all':
                for host in self.index.all('SELECT id FROM hosts'):
                    self._pause(host['id'])
                return {'changed': True}
            if operation == 'directory':
                path = directory(arguments['path'])
                self.index.set_setting('directory', str(path))
                self.last_disk = 0
                return {'path': str(path)}
            if operation == 'credential':
                if self.busy():
                    raise LiveError('请先暂停全部直播监控，再更换或删除凭据')
                value = arguments['value'].strip()
                try:
                    if self.secret_write:
                        self.secret_write(value)
                    else:
                        from ..secrets import set_key_verified
                        set_key_verified('douyin-live', value)
                except Exception:
                    raise LiveError('凭据未能保存或删除，请重试；未改变监控配置') from None
                self.credential = ''
                self.index.set_setting('credential', 'configured' if value else '')
                return {'configured': bool(value)}
            if operation == 'detail':
                row = self._recording(arguments['recording_id'])
                segments = self.index.all('SELECT * FROM segments WHERE recording_id=? ORDER BY started', (row['id'],))
                if any(s['valid'] and not s['started'] for s in segments):
                    # Recovered files have no proven wall time; part numbers retain their order.
                    segments.sort(key=lambda s: s['path'])
                if row['ended'] is None:
                    for path in directory(row['directory']).glob('part-*.ts'):
                        if path.is_file() and not path.is_symlink() and all(s['path'] != str(path) for s in segments):
                            segments.append({'id': '', 'path': str(path), 'started': 0,
                                'duration': 0, 'bytes': path.stat().st_size, 'valid': 0, 'closed': 0})
                host = self.index.one('SELECT status FROM hosts WHERE id=?', (row['host_id'],))
                return {**row, **self._progress(row, host['status'] if host else ''), 'segments': segments}
            if operation == 'export':
                return self._export(arguments['segment_id'], token, arguments['format'], arguments['path'])
            if operation == 'export_status':
                result = self.exports.get(arguments['export_id'])
                if result is None:
                    raise LiveError('导出记录不存在，请重新选择分段')
                return {k: result[k] for k in ('state', 'path', 'message')}
        raise LiveError('直播操作无效')

    def _transcribe(self, arguments):
        token, sid = arguments['session'], arguments['segment_id']
        with self.lock:
            self._current(token)
            if self.closing:
                raise LiveError('Lulu 正在结束直播任务，请稍后操作')
            segment = self._completed_segment(sid)
            rid = segment['recording_id']
            recording = self._recording(rid)
            if rid in self.transcribing:
                raise LiveError('本场录像正在准备文案任务，请稍后再试')
            # Reserve only this recording. File I/O must not hold the controls
            # lock or the CSV lock needed by unrelated ongoing recordings.
            self.transcribing.add(rid)
        try:
            path = managed_file(segment['path'], recording['directory'])
            if not 0 < path.stat().st_size <= 2 * 1024**3:
                raise LiveError('分段超过本地任务的 2 GB 上限，原录像保留')
            if fingerprint(path) != segment['sha256']:
                raise LiveError('分段文件已变化，请先检查；未创建任务')
            with self.lock:
                self._current(token)
                if self.closing:
                    raise LiveError('Lulu 正在结束直播任务，未创建文案任务')
                if (self._completed_segment(sid) != segment or
                        self._recording(rid)['directory'] != recording['directory']):
                    raise LiveError('分段记录已变化，请重新选择；未创建任务')
                return {'path': str(path)}
        finally:
            with self.lock:
                self.transcribing.discard(rid)

    def _host(self, hid):
        row = self.index.one('SELECT * FROM hosts WHERE id=?', (hid,))
        if row is None:
            raise LiveError('主播已移除，请刷新列表')
        return row

    def _recording(self, rid, *, admitted_read=False):
        if rid in self.deleting and not admitted_read:
            raise LiveError('本场录像正在移入废纸篓，请稍后操作')
        row = self.index.one('SELECT * FROM recordings WHERE id=?', (rid,))
        if row is None:
            raise LiveError('录像记录不存在')
        return row

    def _delete_blocked(self, row):
        rid, folder = row['id'], Path(row['directory'])
        if row['ended'] is None or any(m.get('recording_id') == rid and m.get('thread')
                                       for m in self.monitors.values()):
            return '本场录像正在录制或收尾，结束后才能删除'
        if rid in self.deleting:
            return '本场录像正在移入废纸篓'
        if rid in self.transcribing:
            return '本场录像正在准备文案任务，请完成后删除'
        if rid in self.data_checks:
            return '本场直播数据正在保存，请稍后删除'
        for work in self.exports.values():
            if work['state'] == 'running' and (rid in work.get('recording_ids', ()) or
                    (work.get('target') and Path(work['target']).is_relative_to(folder))):
                return '本场录像正在导出或检查，请完成后删除'
        return ''

    def _authorize_recording_folder(self, row):
        rid = row['id']
        # The signed job ledger is the authority, not an arbitrary GUI path
        # or a changed media-index row. This also accepts an old save location.
        try:
            job = self.control.show(row['job_id'])
            policy = job.frozen_policy
            if job.job_kind != KIND or job.state in ACTIVE:
                raise LiveError('本场录像的登记不一致或任务未结束，文件保留')
        except JobLedgerError as exc:
            # A new generation has no old mutable jobs. Only its sealed
            # terminal folder registration can authorize this exact folder.
            # Integrity/storage failures must never fall back to history.
            policy = self.historical_recordings.get(row['job_id'])
            if exc.code != 'CONFLICT' or exc.reason_code != 'JOB_NOT_FOUND' or policy is None:
                raise LiveError('无法核对本场录像的原始登记，文件保留') from None
        except LiveError:
            raise
        except Exception:
            raise LiveError('无法核对本场录像的原始登记，文件保留') from None
        if (policy.get('recording_id') != rid or
                policy.get('directory') != row['directory'] or policy.get('host_id') != row['host_id']):
            raise LiveError('本场录像的登记不一致或任务未结束，文件保留')

    def _delete_recording(self, arguments):
        token, rid = arguments['session'], arguments['recording_id']
        with self.lock:
            self._current(token)
            if self.closing:
                raise LiveError('Lulu 正在结束直播任务，请稍后操作')
            row = self._recording(rid)
            blocked = self._delete_blocked(row)
            if blocked:
                raise LiveError(blocked)
            self.deleting[rid] = row['directory']
        try:
            # Same serialization as CSV reads/writes, without holding controls.
            with self.data_lock:
                with self.lock:
                    self._current(token)
                    if self.closing:
                        raise LiveError('Lulu 正在结束直播任务，未删除录像')
                folder = Path(row['directory'])
                if not folder.is_absolute() or folder.name != rid:
                    raise LiveError('录像文件夹名称或路径不一致，文件保留')
                if any(p.is_symlink() for p in (folder, *folder.parents)):
                    raise LiveError('请选择实际文件夹，暂不支持符号链接')
                # The user may have already moved files to Trash. Remove only
                # this stale projection, without claiming or authorizing a move.
                # A disconnected parent volume is not an absent recording.
                directory(folder.parent)
                if not folder.exists():
                    try:
                        self.index.forget_recording(rid)
                    except Exception:
                        raise LiveError('失效录像记录未能移除，请稍后重试；未改动文件') from None
                    return {'recording_id': rid, 'trashed': False}
                self._authorize_recording_folder(row)
                folder = directory(folder)
                if folder.stat().st_uid != os.getuid():
                    raise LiveError('录像文件夹名称或归属不一致，文件保留')
                try:
                    move_to_trash(folder)
                except LiveError:
                    raise
                except Exception:
                    raise LiveError('未能移入废纸篓，请检查保存位置；列表保留') from None
                try:
                    self.index.forget_recording(rid)
                except Exception:
                    raise LiveError('录像已移入系统废纸篓，列表未能更新；可在废纸篓找回') from None
            return {'recording_id': rid, 'trashed': True}
        finally:
            with self.lock:
                self.deleting.pop(rid, None)

    def output_dir(self):
        return self.index.setting('directory', str(Path.home() / 'Movies' / 'Lulu直播录制'))

    def _enable(self, host, delay=0):
        previous = self.monitors.get(host['id'])
        if previous and previous.get('thread'):
            return
        if host['id'] in self.checks:
            self.checks[host['id']].set()
        self.monitors[host['id']] = {'next': time.monotonic() + delay,
            'cancel': threading.Event(), 'thread': None, 'owned': None, 'job_id': '',
            'session': self.session, 'limited': 0}
        self._status(host['id'], 'checking')

    def _status(self, hid, state, checked=False):
        if checked:
            self.index.write('UPDATE hosts SET status=? WHERE id=?', (state, hid))
            self._remember_observation(hid, state)
        else:
            self.index.write('UPDATE hosts SET status=? WHERE id=?', (state, hid))

    def _remember_observation(self, hid, state):
        self.index.write('UPDATE hosts SET live_state=?,live_error=?,checked=? WHERE id=?',
            (confirmed_live_state(state), state if state in QUERY_ERRORS else '', time.time(), hid))

    def _pause(self, hid, reason='paused'):
        if hid in self.checks:
            self.checks[hid].set()
        monitor = self.monitors.get(hid)
        if monitor:
            monitor['cancel'].set()
            monitor['reason'] = reason
            self._stop_cancelled(monitor)
            jid = monitor.get('job_id')
            if jid:
                job = self.control.show(jid)
                if job.state in ACTIVE and job.state is not JobState.CANCEL_REQUESTED:
                    self.control.cancel(jid, expected_revision=job.job_revision, changed_at=utc())
            if not monitor.get('thread'):
                self.monitors.pop(hid, None)
        self.index.write('UPDATE hosts SET enabled=0 WHERE id=?', (hid,))
        self._status(hid, 'stopping' if monitor and monitor.get('thread') else reason)

    def cancel_job(self, jid):
        with self.lock:
            for hid, monitor in self.monitors.items():
                if monitor.get('job_id') == jid:
                    self._pause(hid)
                    return
            job = self.control.show(jid)
            if job.state not in TERMINAL_STATES:
                self.control.cancel(jid, expected_revision=job.job_revision, changed_at=utc())

    @staticmethod
    def _stop_cancelled(work):
        # Media validation may still be finishing a closed file. Stop the actual
        # recorder independently, without interrupting that final-file check.
        producer = work.get('producer')
        owned = producer.process if producer else work.get('owned')
        if work['cancel'].is_set() and owned and not work.get('stop_worker'):
            work['stop_worker'] = threading.Thread(target=owned.stop, daemon=True,
                                                    name='lulu-live-stop')
            work['stop_worker'].start()

    def _stop_session(self):
        self.closing = True
        self.credential = ''
        # Physical cancellation precedes every disk write, including full-disk failures.
        for monitor in self.monitors.values():
            monitor['cancel'].set()
            monitor['reason'] = 'app_exit'
        for export in self.exports.values():
            if export['state'] == 'running':
                export['cancel'].set()
        for work in list(self.monitors.values()) + list(self.exports.values()):
            self._stop_cancelled(work)
        for hid, monitor in self.monitors.items():
            jid = monitor.get('job_id')
            if jid:
                job = self.control.show(jid)
                if job.state in ACTIVE and job.state is not JobState.CANCEL_REQUESTED:
                    try:
                        self.control.cancel(jid, expected_revision=job.job_revision, changed_at=utc())
                    except Exception:
                        pass  # Producer cancellation is already guaranteed independently of the ledger.
            try:
                self._status(hid, 'stopping' if monitor.get('thread') else 'paused')
            except Exception:
                pass  # An unavailable index cannot keep a producer running.
    def busy(self):
        return (any(not m['cancel'].is_set() or m.get('thread') for m in self.monitors.values())
                or bool(self.checks) or bool(self.data_checks) or bool(self.deleting)
                or bool(self.transcribing)
                or any(e['state'] == 'running' for e in self.exports.values()))

    def _schedule(self):
        while not self.stop_event.wait(.5):
            try:
                with self.lock:
                    if not self.closing and time.monotonic() >= self.deadline:
                        self._stop_session()
                    if not self.closing:
                        if time.monotonic() - self.last_disk >= 10:
                            self.last_disk = time.monotonic()
                            folder = self.output_dir()
                            try:
                                folder = directory(folder, create=True)
                                self.free = self.disk_free(folder)
                                self.disk_warning = self.free < 5 * 1024**3
                            except (OSError, LiveError):
                                self.free = None
                            # Each recording keeps its own path, including when the
                            # default folder or another recording's disk is unavailable.
                            for hid, monitor in list(self.monitors.items()):
                                if monitor['cancel'].is_set():
                                    continue
                                try:
                                    disk = monitor.get('directory', str(folder))
                                    if self.disk_free(directory(disk)) < 2 * 1024**3:
                                        self._pause(hid, 'disk_full')
                                except (OSError, LiveError):
                                    self._pause(hid, 'write_error')
                        for hid, monitor in list(self.monitors.items()):
                            if (not monitor['cancel'].is_set() and not monitor.get('thread')
                                    and hid not in self.checks and monitor['next'] <= time.monotonic()
                                    and len(self.checks) < 4):
                                self._submit_check(hid, self.session)
                            if (monitor.get('thread') and not monitor['cancel'].is_set()
                                    and monitor.get('data_next', float('inf')) <= time.monotonic()
                                    and monitor['recording_id'] not in self.data_checks
                                    and len(self.checks) + len(self.data_checks) < 4):
                                self._submit_data(hid, monitor, self.session)
                    # Stop owned work even if a recording is currently validating a file.
                    for monitor in list(self.monitors.values()) + list(self.exports.values()):
                        self._stop_cancelled(monitor)
            except Exception:
                # Fail closed. No raw provider/process exception escapes or persists.
                with self.lock:
                    self._stop_session()

    def _submit_check(self, hid, token, identify_only=False):
        if hid in self.checks:
            return
        cancel = self.checks[hid] = threading.Event()
        self.pool.submit(self._check, hid, token, identify_only, cancel)

    def _observe(self, host, credential):
        with self.network_limit:
            value = self.provider.check(host, credential)
        with self.lock:
            self.monitor_bytes += value.received_bytes
        return value

    def _data(self, arguments):
        token, rid = arguments['session'], arguments['recording_id']
        with self.lock:
            self._current(token)
            if self.closing:
                raise LiveError('Lulu 正在结束直播任务，请稍后操作')
            row = self._recording(rid)
        result = {'data_error': '', 'read_error': False}
        with self.data_lock:
            with self.lock:
                self._current(token)
                if self.closing:
                    raise LiveError('Lulu 正在结束直播任务，请稍后操作')
                row = self._recording(rid)
            try:
                result.update(read_data(row))
            except (OSError, ValueError, csv.Error) as exc:
                result.update(samples=[], sample_count=0, valid_count=0, invalid_count=0, read_error=True,
                    peak_viewers=None, last_viewers=None, last_viewers_raw='', title='', folder='', truncated=False,
                    data_error=str(exc) if isinstance(exc, LiveError) else '直播数据未能读取，原文件保留；请检查保存位置')
                try:
                    path = data_path(row['directory'])
                    result['folder'] = str(path.parent) if path else ''
                except (OSError, LiveError):
                    pass
            with self.lock:
                # Finish an admitted read before Trash can move its folder.
                # Stop/reopen still invalidates it before any index access.
                self._current(token)
                if self.closing:
                    raise LiveError('Lulu 正在结束直播任务，请稍后操作')
                row = self._recording(rid, admitted_read=True)
                host = self.index.one('SELECT status FROM hosts WHERE id=?', (row['host_id'],))
                result.update(row)
                result.update(self._progress(row, host['status'] if host else ''))
                monitor = next((m for m in self.monitors.values() if m.get('recording_id') == rid), None)
                if monitor and monitor.get('data_error'):
                    result['data_error'] = monitor['data_error']
                return result

    def _save_data(self, host, observation, monitor, *, recording=None, sampled_at=None):
        try:
            with self.data_lock:
                append_sample(recording or self._recording(monitor['recording_id']), host, observation,
                              sampled_at=sampled_at)
            error = ''
        except Exception:
            # Never let metadata disk/parsing errors enter the video failure path.
            error = '直播数据未能保存，录像继续录制；请检查保存位置或可用空间'
        with self.lock:
            monitor['data_error'] = error

    def _submit_data(self, hid, monitor, token, observation=None):
        rid = monitor['recording_id']
        self.data_checks.add(rid)
        monitor['data_next'] = time.monotonic() + 300
        try:
            self.pool.submit(self._sample_data, hid, rid, monitor, token, observation)
        except Exception:
            self.data_checks.discard(rid)
            monitor['data_error'] = '直播数据查询未能启动，录像继续录制；稍后会重试'

    def _sample_data(self, hid, rid, monitor, token, observation=None):
        from .contracts import Observation
        try:
            with self.lock:
                if (not self._can_run(token) or monitor['cancel'].is_set() or
                        monitor.get('recording_id') != rid or not monitor.get('thread')):
                    return
                host, credential = self._host(hid), self.credential
            try:
                value = observation if observation is not None else self._observe(host, credential)
            except Exception:
                value = Observation('network_error')
            sampled_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            with self.lock:
                if (not self._can_run(token) or monitor['cancel'].is_set() or not monitor.get('thread')
                        or self.monitors.get(hid) is not monitor or monitor.get('recording_id') != rid):
                    return
                # The initial sample reuses an already admitted response. It must
                # not overwrite a newer check that completed while CSV I/O waited.
                if observation is None:
                    self._remember_observation(hid, value.state)
            with self.data_lock:
                with self.lock:
                    # Recheck after waiting for file access. Only an already-admitted
                    # write may finish after pause; its timestamp remains pre-pause.
                    if (not self._can_run(token) or monitor['cancel'].is_set() or not monitor.get('thread')
                            or self.monitors.get(hid) is not monitor or monitor.get('recording_id') != rid):
                        return
                    row = self._recording(rid)
                self._save_data(host, value, monitor, recording=row, sampled_at=sampled_at)
        finally:
            with self.lock:
                self.data_checks.discard(rid)

    def _identity(self, hid, value):
        """Merge simultaneous homepage/room additions before creating a producer."""
        host = self._host(hid)
        sec, rid = value.sec_uid or host['sec_uid'], value.web_rid or host['web_rid']
        duplicate = self.index.one('SELECT * FROM hosts WHERE id!=? AND ((sec_uid=? AND ?!=\'\') OR (web_rid=? AND ?!=\'\'))',
                                   (hid, sec, sec, rid, rid))
        if duplicate:
            # Historical recordings retain the owner in their signed job registration.
            enabled = bool(host['enabled'] or duplicate['enabled'])
            self.index.write('DELETE FROM hosts WHERE id=?', (hid,))
            self.index.write('UPDATE hosts SET enabled=?,sec_uid=?,web_rid=?,name=? WHERE id=?',
                (int(enabled), sec or duplicate['sec_uid'], rid or duplicate['web_rid'],
                 value.name or duplicate['name'], duplicate['id']))
            self._remember_observation(duplicate['id'], value.state)
            monitor = self.monitors.pop(hid, None)
            if monitor:
                monitor['cancel'].set()
            if enabled and duplicate['id'] not in self.monitors and self._can_run(self.session):
                self._enable(self._host(duplicate['id']))
            return None
        self.index.write('UPDATE hosts SET sec_uid=?,web_rid=?,name=? WHERE id=?',
                         (sec, rid, value.name or host['name'], hid))
        return self._host(hid)

    def _check(self, hid, token, identify_only, cancel=None):
        try:
            with self.lock:
                if not self._can_run(token) or (cancel and cancel.is_set()):
                    return
                host = self._host(hid)
                credential = '' if identify_only else self.credential
            value = self._observe(host, credential)
            with self.lock:
                if not self._can_run(token) or (cancel and cancel.is_set()):
                    return
                host = self._identity(hid, value)
                if host is None:
                    return
                if identify_only:
                    self._status(hid, 'paused')
                    self._remember_observation(hid, value.state)
                    return
                monitor = self.monitors.get(hid)
                if not monitor or monitor['cancel'].is_set():
                    return
                self._status(hid, value.state, checked=True)
                if value.state == 'live':
                    self._begin(host, value, monitor, token)
                else:
                    if value.state == 'access_limited':
                        monitor['limited'] += 1
                        delay = min(900, 300 * monitor['limited'])
                    else:
                        delay = 60 + random.uniform(-5, 5)
                    monitor['next'] = time.monotonic() + delay
        except Exception:
            with self.lock:
                if (not (cancel and cancel.is_set()) and self._can_run(token)
                        and hid in self.monitors and not self.monitors[hid]['cancel'].is_set()):
                    self._status(hid, 'parse_error', checked=True)
                    self.monitors[hid]['next'] = time.monotonic() + 60
        finally:
            with self.lock:
                self.checks.pop(hid, None)

    def _begin(self, host, observation, monitor, token):
        if not self.ffmpeg:
            self.ffmpeg = bundled_ffmpeg()
        root = directory(self.output_dir(), create=True)
        if self.disk_free(root) < 2 * 1024**3:
            self._pause(host['id'], 'disk_full')
            return
        rid, jid = uid('recording'), uid('live')
        folder = root / rid
        folder.mkdir(mode=0o700)
        policy = {'host_id': host['id'], 'recording_id': rid, 'directory': str(folder),
                  'segment_seconds': 1800, 'source': host['source']}
        import hashlib
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        job = self.control._store.create_job(job_id=jid, attempt_id=uid('attempt'), lineage_id=uid('lineage'),
            job_kind=KIND, input_ref='source:sha256:' + hashlib.sha256(host['source'].encode()).hexdigest(),
            frozen_policy=policy, created_by='gui', installation_id=self.installation_id,
            canonical_operation_id='OP_GUI_LIVE_RECORDING_V1', idempotency_key=jid,
            intent_payload_digest=digest, created_at=utc(), effect_fingerprint='effect:local-live-media',
            account_ref='account:none', target_ref='target:local-live-media', uncertain_response_state='none')
        try:
            queued = self.control._store.transition(jid, expected_revision=job.job_revision,
                                                   new_state=JobState.QUEUED, changed_at=utc())
            _, lease = self.control.claim_worker(jid, expected_revision=queued.job_revision,
                worker_instance_id='core-live:' + jid, operation_receipt=None, changed_at=utc())
            self.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started) VALUES(?,?,?,?,?,?)',
                             (rid, host['id'], host['name'], jid, str(folder), time.time()))
            monitor.update(job_id=jid, directory=str(folder), recording_id=rid, started=time.time(),
                           last_data_at=time.monotonic(), recording_state='connecting', clock_duration=0,
                           clock_base=0)
            monitor.pop('producer', None)
            thread = threading.Thread(target=self._record, args=(host, observation, monitor, token, lease),
                                      name='lulu-live-recording', daemon=True)
            monitor['thread'] = thread
            thread.start()
            # Start video first. Reuse the initial observation without another request.
            self._submit_data(host['id'], monitor, token, observation)
        except Exception:
            monitor['thread'], monitor['job_id'] = None, ''
            current = self.control.show(jid)
            self.control._store.transition(jid, expected_revision=current.job_revision,
                new_state=JobState.FAILED, changed_at=utc(), reason_code='LIVE_START_FAILED',
                lease_token=self.control._store.current_lease_token(jid) if current.attempts[-1].worker_instance_id else None)
            self.index.write('UPDATE recordings SET ended=?,reason=? WHERE id=?', (time.time(), 'recording_error', rid))
            raise

    def _owned(self, monitor, process, *, finalize=False):
        with self.lock:
            if not finalize and (monitor['cancel'].is_set() or not self._can_run(monitor['session'])):
                process.stop()
                raise LiveError('操作已停止，原文件保留')
            monitor['owned'] = process
            monitor['validating'] = finalize
            if not finalize:
                monitor.pop('stop_worker', None)

    def _save_segments(self, monitor, producer, lease):
        for path, start in producer.completed():
            if self.index.one('SELECT id FROM segments WHERE path=?', (str(path),)):
                continue
            try:
                value = probe(managed_file(path, monitor['directory']), self.ffmpeg,
                              register=lambda p: self._owned(monitor, p, finalize=True))
                valid = 1
            except LiveError:
                value = {'duration': 0, 'bytes': path.stat().st_size if path.is_file() else 0, 'sha256': ''}
                valid = 0
            finally:
                with self.lock:
                    monitor['validating'] = False
            self.control.verify_worker_lease(monitor['job_id'], lease_token=lease, mutation_kind='result')
            self.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                (uid('segment'), monitor['recording_id'], str(path), start, value['duration'],
                 value['bytes'], 1, valid, value['sha256']))

    @staticmethod
    def _choice(value, failures):
        streams = value.streams
        if not streams:
            return None
        bitrates = sorted({s.bitrate for s in streams}, reverse=True)
        level = min(max(0, (failures - 4) // 2), len(bitrates) - 1)
        choices = [s for s in streams if s.bitrate == bitrates[level]]
        protocol = 'hls' if failures >= 2 else 'flv'
        return next((s for s in choices if s.protocol == protocol), choices[0])

    def _record(self, host, value, monitor, token, lease):
        jid, rid = monitor['job_id'], monitor['recording_id']
        producer = None
        reason, failures, retry_count, gap = 'recording_error', 0, 0, None
        gaps = []
        try:
            while not monitor['cancel'].is_set() and self._can_run(token):
                self.control.verify_worker_lease(jid, lease_token=lease, mutation_kind='result')
                if time.monotonic() - monitor['last_data_at'] >= RECONNECT_TIMEOUT:
                    reason = 'reconnect_timeout'
                    break
                if value.state == 'offline':
                    if monitor['cancel'].wait(15):
                        break
                    value = self._observe(self._host(host['id']), self.credential)
                    with self.lock:
                        if monitor['cancel'].is_set() or not self._can_run(token):
                            break
                        self._remember_observation(host['id'], value.state)
                    if value.state == 'offline':
                        reason = 'offline'
                        break
                if value.state == 'live' and value.streams:
                    numbers = [int(p.stem.split('-')[-1]) for p in Path(monitor['directory']).glob('part-*.ts')]
                    with self.lock:
                        if monitor['cancel'].is_set() or not self._can_run(token):
                            break
                        self.control.verify_worker_lease(jid, lease_token=lease, mutation_kind='file')
                        producer = self.producer_factory(self.ffmpeg, self._choice(value, failures),
                            monitor['directory'], start_number=max(numbers, default=-1) + 1)
                        self._owned(monitor, producer.process)
                        monitor['producer'] = producer
                        monitor['recording_state'] = 'connecting'
                        saved = self.index.one('SELECT COALESCE(SUM(duration),0) AS duration FROM segments '
                                               'WHERE recording_id=? AND valid=1', (rid,))
                        monitor['clock_base'] = saved['duration']
                    attempt = time.monotonic()
                    self._status(host['id'], 'live')
                    while producer.process.process.poll() is None and not monitor['cancel'].wait(.5):
                        if not self._can_run(token):
                            break
                        growing = producer.growing()
                        if producer.has_data:
                            monitor['last_data_at'] = max(monitor['last_data_at'], producer.last_growth)
                        if not growing or time.monotonic() - monitor['last_data_at'] >= RECONNECT_TIMEOUT:
                            break
                        monitor['recording_state'] = 'live' if producer.has_data else 'connecting'
                        if gap is not None and producer.has_data:
                            gaps.append([gap, max(gap, time.time())])
                            gap = None
                            self.index.write('UPDATE recordings SET gaps=? WHERE id=?', (json.dumps(gaps), rid))
                        self._save_segments(monitor, producer, lease)
                        with self.lock:
                            monitor['owned'] = producer.process
                    monitor['recording_state'] = 'stopping'
                    producer.stop()
                    self._save_segments(monitor, producer, lease)
                    if monitor['cancel'].is_set() or not self._can_run(token):
                        break
                    if time.monotonic() - attempt < 12:
                        failures += 1
                    else:
                        failures = 0
                if gap is None:
                    row = self.index.one('SELECT MAX(started+duration) AS ended FROM segments WHERE recording_id=? AND valid=1', (rid,))
                    gap = min(time.time(), row['ended']) if row and row['ended'] else time.time()
                self._status(host['id'], 'reconnecting' if value.state == 'live' else value.state)
                monitor['recording_state'] = 'reconnecting'
                retry = (3, 10, 30, 60)[min(retry_count, 3)]
                if value.state in {'access_limited', 'login_required'}:
                    retry = min(900, 300 * (failures + 1))
                retry = min(retry, max(0, RECONNECT_TIMEOUT - (time.monotonic() - monitor['last_data_at'])))
                if monitor['cancel'].wait(retry) or not self._can_run(token):
                    break
                if time.monotonic() - monitor['last_data_at'] >= RECONNECT_TIMEOUT:
                    reason = 'reconnect_timeout'
                    break
                value = self._observe(self._host(host['id']), self.credential)
                with self.lock:
                    if monitor['cancel'].is_set() or not self._can_run(token):
                        break
                    self._remember_observation(host['id'], value.state)
                retry_count += 1
        except OSError:
            reason = 'write_error'
            with self.lock:
                self._pause(host['id'], reason)
        except Exception:
            reason = 'recording_error'
        finally:
            monitor['recording_state'] = 'stopping'
            if producer:
                producer.stop()
            with self.lock:
                reason = monitor.get('reason', reason)
                if gap is not None and reason != 'offline':
                    gaps.append([gap, time.time()])
                try:
                    # Unlisted files have no proven start time. Zero means unknown,
                    # including after recovery validates their actual media duration.
                    for path in Path(monitor['directory']).glob('part-*.ts'):
                        if not self.index.one('SELECT id FROM segments WHERE path=?', (str(path),)) and path.is_file() and not path.is_symlink():
                            self.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                                (uid('segment'), rid, str(path), 0, 0, path.stat().st_size, 0, 0, ''))
                    if reason == 'offline' and not self.index.one('SELECT id FROM segments WHERE recording_id=? AND valid=1 LIMIT 1', (rid,)):
                        reason = 'recording_error'
                    self.index.write('UPDATE recordings SET ended=?,reason=?,gaps=? WHERE id=?',
                                     (time.time(), reason, json.dumps(gaps), rid))
                except Exception:
                    reason = 'recovery_required'
                try:
                    current = self.control.show(jid)
                    if current.state is JobState.CANCEL_REQUESTED:
                        self.control.settle_cancelled(jid, expected_revision=current.job_revision, changed_at=utc(), lease_token=lease)
                    elif current.state is JobState.RUNNING:
                        self.control.complete_worker(jid, expected_revision=current.job_revision, lease_token=lease,
                            new_state=JobState.SUCCEEDED if reason == 'offline' else JobState.FAILED,
                            result_ref=None, changed_at=utc())
                except Exception:
                    reason = 'recovery_required'
                finally:
                    monitor['thread'], monitor['owned'], monitor['job_id'] = None, None, ''
                    monitor['next'] = time.monotonic() + 60
                    if monitor['cancel'].is_set() or reason == 'recovery_required':
                        monitor['cancel'].set()
                        self.monitors.pop(host['id'], None)
                    try:
                        final_state = ('offline' if reason == 'offline' else
                                       value.state if reason == 'reconnect_timeout' and value.state in {
                                           'access_limited', 'login_required', 'network_error', 'parse_error'} else
                                       'parse_error' if reason == 'reconnect_timeout' else
                                       monitor.get('reason', 'network_error'))
                        self._status(host['id'], final_state)
                        if monitor.get('remove'):
                            self.index.write('DELETE FROM hosts WHERE id=?', (host['id'],))
                    except Exception:
                        pass

    def _completed_segment(self, sid):
        segment = self.index.one('SELECT * FROM segments WHERE id=?', (sid,))
        if not segment or not segment['closed'] or not segment['valid']:
            raise LiveError('请选择已完成且可用的分段')
        return segment

    def _export(self, sid, token, format, target):
        segment = self._completed_segment(sid)
        if sum(e['state'] == 'running' for e in self.exports.values()) >= 2:
            raise LiveError('已有两项导出正在处理，请稍后再试')
        if not self.ffmpeg:
            self.ffmpeg = bundled_ffmpeg()
        recording = self._recording(segment['recording_id'])
        if any(Path(target).is_relative_to(Path(folder)) for folder in self.deleting.values()):
            raise LiveError('导出位置正在移入废纸篓，请选择其他文件夹')
        eid = uid('export')
        work = {'state': 'running', 'path': '', 'message': '', 'cancel': threading.Event(),
                'owned': None, 'session': token, 'recording_ids': {recording['id']}, 'target': target}
        self.exports[eid] = work
        # Bound completed in-memory receipts; files remain registered on disk.
        for key in list(self.exports):
            if len(self.exports) > 100 and self.exports[key]['state'] != 'running':
                del self.exports[key]
        def run():
            try:
                path = export_media(segment, recording, self.ffmpeg, format=format, target=target,
                                    register=lambda p: self._owned(work, p))
                with self.lock:
                    work.update(state='succeeded', path=path)
            except LiveError as exc:
                with self.lock:
                    work.update(state='failed', message=str(exc))
            except Exception:
                with self.lock:
                    work.update(state='failed', message='导出失败，原录像保留')
            finally:
                with self.lock:
                    work['owned'] = None
        threading.Thread(target=run, name='lulu-live-export', daemon=True).start()
        return {'export_id': eid}

    def _background_recovery(self, rows, token):
        eid = uid('recovery')
        work = {'state': 'running', 'path': '', 'message': '', 'cancel': threading.Event(),
                'owned': None, 'session': token, 'recording_ids': {row['id'] for row in rows}}
        self.exports[eid] = work
        def run():
            remaining = {row['id']: row for row in rows}
            try:
                ffmpeg = self.ffmpeg or bundled_ffmpeg()
                for row in rows:
                    if work['cancel'].is_set():
                        break
                    try:
                        # glob may suppress an unreadable-directory error and look
                        # like an empty successful scan. Keep failed scans pending.
                        with os.scandir(directory(row['directory'])) as entries:
                            paths = [Path(entry.path) for entry in entries
                                     if entry.name.startswith('part-') and entry.name.endswith('.ts')]
                        for path in paths:
                            if work['cancel'].is_set():
                                break
                            existing = self.index.one('SELECT * FROM segments WHERE path=?', (str(path),))
                            if existing and existing['valid']:
                                continue
                            try:
                                info = probe(managed_file(path, row['directory']), ffmpeg, register=lambda p: self._owned(work, p))
                            except LiveError:
                                info = {'duration': 0, 'bytes': path.stat().st_size, 'sha256': ''}
                            if work['cancel'].is_set():
                                break
                            # Completed-list registrations already have a start;
                            # unfinished/unlisted files have no proven wall time.
                            start = existing['started'] if existing and existing['closed'] else 0
                            self.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                                'started=excluded.started,duration=excluded.duration,bytes=excluded.bytes,closed=1,valid=excluded.valid,sha256=excluded.sha256',
                                (uid('segment'), row['id'], str(path), start, info['duration'],
                                 info['bytes'], 1, int(bool(info['duration'])), info['sha256']))
                        if work['cancel'].is_set():
                            break
                        self.index.write("UPDATE recordings SET reason='recovery_checked' WHERE id=? AND reason='recovery_required'",
                                         (row['id'],))
                        remaining.pop(row['id'])
                    except Exception:
                        # An unavailable volume or failed index write must not
                        # prevent recovery of other recordings in this batch.
                        continue
            except Exception:
                pass  # The durable pending records remain eligible on the next start.
            finally:
                with self.lock:
                    queued = {row['id'] for row in self.recovery}
                    self.recovery.extend(row for key, row in remaining.items() if key not in queued)
                    work['state'] = ('cancelled' if work['cancel'].is_set() else
                                     'failed' if remaining else 'succeeded')
                    if remaining:
                        work['message'] = '部分录像尚未检查完成，原文件保留；恢复磁盘连接后重新打开 Lulu 可重试'
                    work['owned'] = None
        threading.Thread(target=run, name='lulu-live-recovery', daemon=True).start()

    def _progress(self, recording, status=''):
        info = self.index.one('SELECT COUNT(*) AS n,COALESCE(SUM(bytes),0) AS bytes,COALESCE(SUM(duration),0) AS duration '
                             'FROM segments WHERE recording_id=?', (recording['id'],))
        monitor = next((m for m in self.monitors.values() if m.get('recording_id') == recording['id']), None)
        producer = monitor.get('producer') if monitor else None
        running = bool(recording['ended'] is None and producer and producer.has_data
                       and not monitor['cancel'].is_set() and producer.process.process.poll() is None
                       and time.monotonic() - producer.last_growth < 30
                       and status == 'live')
        if producer and recording['ended'] is None:
            info['bytes'] = max(info['bytes'], producer.last_bytes)
            info['n'] = max(info['n'], producer.file_count)
            if running:
                info['duration'] = max(info['duration'], monitor.get('clock_base', 0) +
                    max(0, getattr(producer, 'duration', 0)))
                monitor['clock_duration'] = info['duration']
            else:
                info['duration'] = max(info['duration'], monitor.get('clock_duration', 0))
        state = ('ended' if recording['ended'] is not None else
                 'stopping' if monitor and (monitor['cancel'].is_set() or monitor.get('recording_state') == 'stopping') else
                 'live' if running else
                 'connecting' if monitor and monitor.get('recording_state') == 'connecting'
                                 and (not producer or producer.process.process.poll() is None) else 'reconnecting')
        return {**info, 'clock_running': running, 'recording_state': state}

    def snapshot(self, tab, query, filter, page):
        values = []
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        if tab == 'hosts':
            where = '(name LIKE ? ESCAPE \'\\\' OR source LIKE ? ESCAPE \'\\\')'
            values = ['%' + escaped + '%'] * 2
            if filter in LIVE_MESSAGES:
                where += ' AND live_state=?'
                values.append(filter)
            elif filter == 'enabled':
                where += ' AND enabled=1'
            elif filter == 'paused':
                where += ' AND enabled=0'
            total = self.index.one('SELECT COUNT(*) AS n FROM hosts WHERE ' + where, values)['n']
            page = min(page, max(1, (total + 19) // 20))
            rows = self.index.all('SELECT * FROM hosts WHERE ' + where + ' ORDER BY created DESC LIMIT 20 OFFSET ?',
                                  (*values, (page - 1) * 20))
            for row in rows:
                row['live_state'] = confirmed_live_state(row['live_state'])
                row['message'] = LIVE_MESSAGES[row['live_state']]
                row['live_url'] = 'https://live.douyin.com/' + row['web_rid'] if row['web_rid'] else ''
                recording = self.index.one('SELECT * FROM recordings WHERE host_id=? AND ended IS NULL', (row['id'],))
                row.update(self._progress(recording, row['status']) if recording else
                           {'duration': 0, 'bytes': 0, 'n': 0, 'clock_running': False})
                row['progress_message'] = (recording_message({**recording, 'recording_state': row['recording_state']}) if recording else
                                           '监控中' if row['enabled'] else '已暂停')
                monitor = self.monitors.get(row['id'], {})
                errors = [MESSAGES.get(row['live_error'], ''),
                          MESSAGES.get(row['status'], '') if row['status'] in
                          {'disk_full', 'write_error', 'credential_error',
                           'recovery_required', 'recovery_checked'} else '',
                          monitor.get('data_error', '')]
                row['error_message'] = '\n'.join(dict.fromkeys(error for error in errors if error))
        else:
            where = 'name LIKE ? ESCAPE \'\\\''
            values = ['%' + escaped + '%']
            if filter in {'today', 'week', 'month'}:
                start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                where += ' AND started>=?'
                values.append(start if filter == 'today' else time.time() - (7 if filter == 'week' else 30) * 86400)
            total = self.index.one('SELECT COUNT(*) AS n FROM recordings WHERE ' + where, values)['n']
            page = min(page, max(1, (total + 19) // 20))
            rows = self.index.all('SELECT * FROM recordings WHERE ' + where + ' ORDER BY started DESC LIMIT 20 OFFSET ?',
                                  (*values, (page - 1) * 20))
            for row in rows:
                host = self.index.one('SELECT status FROM hosts WHERE id=?', (row['host_id'],))
                row.update(self._progress(row, host['status'] if host else ''))
                row['delete_blocked'] = self._delete_blocked(row)
        count_hosts = self.index.one('SELECT COUNT(*) AS n FROM hosts')['n']
        count_records = self.index.one('SELECT COUNT(*) AS n FROM recordings')['n']
        recording_count = 0
        for monitor in self.monitors.values():
            if monitor.get('thread'):
                row = self.index.one('SELECT * FROM recordings WHERE id=?', (monitor.get('recording_id'),))
                if row:
                    host = self.index.one('SELECT status FROM hosts WHERE id=?', (row['host_id'],))
                    recording_count += self._progress(row, host['status'] if host else '')['clock_running']
        return {'rows': rows, 'total': total, 'page': page, 'pages': max(1, (total + 19) // 20),
            'hosts': count_hosts, 'recordings': count_records, 'directory': self.output_dir(),
            'credential': bool(self.index.setting('credential')), 'active': self.busy(),
            'monitoring': sum(not m['cancel'].is_set() for m in self.monitors.values()),
            'recording': recording_count,
            'free_bytes': self.free, 'disk_warning': self.disk_warning,
            'monitor_mb_hour': self.monitor_bytes / max(1,time.monotonic()-self.started_at) * 3600 / 1024**2 if self.monitor_bytes else None}

    def shutdown(self):
        with self.lock:
            self._stop_session()
        limit = time.monotonic() + 20
        while self.busy() and time.monotonic() < limit:
            time.sleep(.05)
        if self.busy():
            raise LiveError('直播任务仍在收尾，未关闭索引')
        self.stop_event.set()
        self.scheduler.join(2)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.index.close()
