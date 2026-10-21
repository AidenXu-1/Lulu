"""One asynchronous GUI session independent of which page is visible."""
from collections import deque
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..core.live_recording.client import dispatch
from ..core.live_recording.contracts import LiveError, ORDERED_CONTROLS

class LiveRequest(QThread):
    replied = Signal(object)
    failed = Signal(str)

    def __init__(self, dispatcher, operation, arguments, parent):
        super().__init__(parent)
        self.dispatcher, self.operation, self.arguments = dispatcher, operation, arguments

    def run(self):
        try:
            self.replied.emit(self.dispatcher(self.operation, self.arguments))
        except LiveError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit('直播功能连接未就绪，请重新打开 Lulu；已有资料保留')

class LiveController(QObject):
    snapshot = Signal(object)
    error = Signal(str)
    activity_changed = Signal()
    stopped = Signal()

    def __init__(self, parent=None, *, dispatcher=dispatch):
        super().__init__(parent)
        self.dispatcher = dispatcher
        self.session, self.nonce = '', uuid4().hex
        self.active = False
        self.connected = False
        self.closing = False
        self.settled = False
        self.worker = None
        self.data_worker = None
        self.delete_worker = None
        self.transcribe_worker = None
        self.comments_worker = None
        self.login_worker = None
        self.start_worker = None
        self.directory_worker = None
        self._sequence = 0
        self.core_stopped = False
        self.queue = deque()
        self.params = None
        self.heartbeat_at = 0
        self.poll_at = 0
        self._connection_epoch = 0
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def start(self):
        if self.connected or self.worker or self.closing:
            return
        # Restored monitor intent is unknown until Core's first heartbeat reply.
        self.active = True
        self.activity_changed.emit()
        def opened(reply):
            self.session = reply['session']
            self.connected = True
            self.timer.start()
            self._tick()
        self._enqueue('open', {'nonce': self.nonce}, opened)

    def reconnect(self):
        """Refresh after the idle Core was replaced; never replay old operations."""
        if self.closing or self.settled:
            return
        self._connection_epoch += 1
        self.connected, self.session = False, ''
        self.timer.stop()
        self._discard_controls()
        self.heartbeat_at = self.poll_at = 0
        if not self.worker:
            self.start()

    def call(self, operation, callback=None, failure=None, **arguments):
        if not self.connected or self.closing:
            self.error.emit('直播连接未就绪，请等待启动准备完成')
            if failure:
                failure()
            return
        if operation == 'start' and (self.directory_worker or any(item[0] == 'directory' for item in self.queue)):
            self.error.emit('保存位置正在检查，请等待完成后再开始监控')
            if failure:
                failure()
            return
        if operation in ORDERED_CONTROLS:
            self._sequence += 1
            arguments['sequence'] = self._sequence
        if operation in {'pause', 'pause_all', 'remove'} and self.start_worker:
            if operation == 'pause_all' or arguments['host_id'] == self.start_worker.arguments['host_id']:
                self.start_worker.discard_reply()
        if operation in {'pause', 'pause_all', 'remove'}:
            retained = deque()
            for item in self.queue:
                if item[0] == 'start' and (operation == 'pause_all' or arguments['host_id'] == item[1]['host_id']):
                    if item[3]:
                        item[3]()
                else:
                    retained.append(item)
            self.queue = retained
        if operation in {'start', 'add', 'export', 'directory'}:
            self.active = True  # Conservatively guard installation until Core replies.
            self.activity_changed.emit()
        if operation in {'data', 'delete_recording', 'transcribe', 'comments', 'comments_export'}:
            self._local_file_request(operation, {'session': self.session, **arguments}, callback, failure)
            return
        self._enqueue(operation, {'session': self.session, **arguments}, callback, failure=failure)

    def _local_file_request(self, operation, arguments, callback, failure):
        # Bound each kind of file I/O independently. Slow external-volume CSV or
        # Trash operations must not delay heartbeat, pause or application stop.
        slot = {'data': 'data_worker', 'delete_recording': 'delete_worker', 'transcribe': 'transcribe_worker',
                'comments': 'comments_worker', 'comments_export': 'comments_worker',
                'start': 'start_worker', 'directory': 'directory_worker'}[operation]
        if getattr(self, slot):
            self.error.emit({'data': '直播数据正在读取，请稍后再试',
                             'delete_recording': '录像正在移入废纸篓，请稍后再试',
                             'transcribe': '正在检查录像分段，请稍后再试',
                             'comments': '弹幕正在读取或导出，请稍后再试',
                             'comments_export': '弹幕正在读取或导出，请稍后再试',
                             'start': '正在检查保存位置，请稍后开始其他监控',
                             'directory': '保存位置正在检查，请稍后重试'}[operation])
            if failure:
                failure()
            return
        if operation in {'delete_recording', 'transcribe', 'comments_export'}:
            self.active = True
            self.activity_changed.emit()
        epoch = self._connection_epoch
        worker = LiveRequest(self.dispatcher, operation, arguments, self)
        setattr(self, slot, worker)
        completed = False
        def discard_reply():
            nonlocal completed
            if not completed:
                completed = True
                if failure:
                    failure()
        worker.discard_reply = discard_reply
        def replied(result):
            nonlocal completed
            if completed:
                return
            if (epoch == self._connection_epoch and not self.closing
                    and self.session == arguments['session']):
                completed = True
                if callback:
                    callback(result)
            else:
                discard_reply()
        def failed(message):
            if completed:
                return
            if epoch == self._connection_epoch and not self.closing:
                self.error.emit(message)
            discard_reply()
        def finished():
            setattr(self, slot, None)
            worker.deleteLater()
            self._finish_stop()
        worker.replied.connect(replied)
        worker.failed.connect(failed)
        worker.finished.connect(finished)
        worker.start()

    def _finish_stop(self):
        if (self.core_stopped and not self.worker and not self.data_worker and not self.delete_worker
                and not self.transcribe_worker and not self.comments_worker and not self.login_worker
                and not self.start_worker and not self.directory_worker and not self.settled):
            self.settled = True
            self.stopped.emit()

    @property
    def has_pending_work(self):
        passive = {'open', 'snapshot', 'heartbeat', 'stop'}
        return bool(self.login_worker or self.start_worker or self.directory_worker
                    or (self.worker and self.worker.operation not in passive)
                    or any(item[0] not in passive for item in self.queue))

    def view(self, **params):
        self.params = params
        self.poll_at = 0
        self.queue = deque(item for item in self.queue if item[0] != 'snapshot')
        self._tick()

    def _discard_controls(self):
        pending = tuple(self.queue)
        self.queue.clear()
        for _, _, _, failure in pending:
            if failure:
                failure()
        if self.worker and self.worker.operation not in {'open', 'heartbeat', 'stop'}:
            self.worker.discard_reply()
        for worker in (self.start_worker, self.directory_worker):
            if worker:
                worker.discard_reply()

    def _enqueue(self, operation, arguments, callback=None, *, first=False, failure=None):
        if operation == 'snapshot':
            # Keep only the newest requested view, never a backlog of polls.
            self.queue = deque(item for item in self.queue if item[0] != 'snapshot')
        if len(self.queue) >= 12:
            self.error.emit('操作正在处理，请稍后再试')
            if failure:
                failure()
            return
        item = (operation, arguments, callback, failure)
        if first:
            self.queue.appendleft(item)
        elif operation in {'start', 'pause', 'pause_all', 'comments_setting'}:
            # User actions keep their order and precede queued background polls.
            at = next((i for i, queued in enumerate(self.queue) if queued[0] == 'snapshot'), len(self.queue))
            self.queue.insert(at, item)
        else:
            self.queue.append(item)
        self._pump()

    def _pump(self):
        if self.worker or not self.queue:
            return
        operation, arguments, callback, failure = self.queue.popleft()
        if operation in {'start', 'directory'}:
            # First honor connection/heartbeat ordering, then release the control
            # lane immediately; slow volume work has its own bounded request slot.
            self._local_file_request(operation, arguments, callback, failure)
            self._pump()
            return
        epoch = self._connection_epoch
        worker = LiveRequest(self.dispatcher, operation, arguments, self)
        self.worker = worker
        completed = False
        def discard_reply():
            nonlocal completed
            if completed:
                return
            completed = True
            if failure:
                failure()
        worker.discard_reply = discard_reply
        def replied(result):
            nonlocal completed
            if completed:
                return
            if epoch != self._connection_epoch:
                discard_reply()
                return
            completed = True
            if operation in {'heartbeat', 'stop'}:
                self.active = bool(result['active'])
                self.activity_changed.emit()
                if operation == 'heartbeat' and result['stopped'] and not self.closing:
                    # An expired lease remains closed even after its work ends.
                    # Open a fresh session; never replay the expired queue.
                    pending = any(item[0] not in {'heartbeat', 'snapshot'} for item in self.queue)
                    self.reconnect()
                    if pending:
                        self.error.emit('直播连接已中断，未执行刚才的操作；连接恢复后请重试')
                    return
                if self.closing and result['stopped']:
                    self.core_stopped = True
                    self.timer.stop()
                    self.queue.clear()
            if operation == 'snapshot' and self.params == {k: v for k, v in arguments.items() if k != 'session'}:
                self.active = bool(result['active'])
                self.activity_changed.emit()
                self.snapshot.emit(result)
            if callback:
                callback(result)
        def failed(message):
            if epoch != self._connection_epoch:
                discard_reply()
                return
            discard_reply()
            self.error.emit(message)
            if operation == 'open':
                self.connected = False
            if self.closing:
                # Heartbeat loss is a fallback, never claim actual process settlement.
                self.timer.start()
        worker.replied.connect(replied)
        worker.failed.connect(failed)
        def finished():
            self.worker = None
            worker.deleteLater()
            # Finish this request before starting a new connection. A second
            # finished-signal connection can be lost when deleteLater runs.
            if epoch != self._connection_epoch:
                self.start()
            else:
                self._pump()
            self._finish_stop()
        worker.finished.connect(finished)
        worker.start()

    def _tick(self):
        if not self.connected or self.settled or self.core_stopped:
            return
        now = time.monotonic()
        operations = {item[0] for item in self.queue}
        if self.worker and self.worker.operation in {'heartbeat', 'stop'}:
            operations.add(self.worker.operation)
        if (self.worker and self.worker.operation == 'snapshot'
                and self.params == {k: v for k, v in self.worker.arguments.items() if k != 'session'}):
            operations.add('snapshot')
        if self.closing:
            if 'stop' not in operations and not (self.worker and self.worker.operation == 'stop'):
                self._enqueue('stop', {'session': self.session}, first=True)
            return
        if now - self.heartbeat_at >= 7 and 'heartbeat' not in operations:
            self.heartbeat_at = now
            self._enqueue('heartbeat', {'session': self.session}, first=True)
        if self.params and now - self.poll_at >= 2 and 'snapshot' not in operations:
            self.poll_at = now
            self._enqueue('snapshot', {'session': self.session, **self.params})

    def request_stop(self):
        if self.settled:
            return True
        self.closing = True
        self._discard_controls()
        if self.login_worker:
            self.login_worker.requestInterruption()
        if not self.session:
            # Open may still be in flight. Its callback starts the stop timer.
            if not self.worker:
                self.core_stopped = True
                self.timer.stop()
                self._finish_stop()
                return self.settled
            return False
        self.timer.start()
        self._tick()
        return False
