"""One asynchronous GUI session independent of which page is visible."""
from collections import deque
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt

from ..core.live_recording.client import dispatch
from ..core.live_recording.contracts import LiveError

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
        self.queue.clear()
        self.heartbeat_at = self.poll_at = 0
        if self.worker:
            self.worker.finished.connect(self.start, Qt.SingleShotConnection)
        else:
            self.start()

    def call(self, operation, callback=None, failure=None, **arguments):
        if not self.connected or self.closing:
            self.error.emit('直播连接未就绪，请等待启动准备完成')
            if failure:
                failure()
            return
        if operation in {'data', 'delete_recording', 'transcribe'}:
            self._local_file_request(operation, {'session': self.session, **arguments}, callback, failure)
            return
        if operation in {'start', 'add', 'export'}:
            self.active = True  # Conservatively guard installation until Core replies.
            self.activity_changed.emit()
        self._enqueue(operation, {'session': self.session, **arguments}, callback)

    def _local_file_request(self, operation, arguments, callback, failure):
        # Bound each kind of file I/O independently. Slow external-volume CSV or
        # Trash operations must not delay heartbeat, pause or application stop.
        slot = {'data': 'data_worker', 'delete_recording': 'delete_worker', 'transcribe': 'transcribe_worker'}[operation]
        if getattr(self, slot):
            self.error.emit({'data': '直播数据正在读取，请稍后再试',
                             'delete_recording': '录像正在移入废纸篓，请稍后再试',
                             'transcribe': '正在检查录像分段，请稍后再试'}[operation])
            if failure:
                failure()
            return
        if operation in {'delete_recording', 'transcribe'}:
            self.active = True
            self.activity_changed.emit()
        worker = LiveRequest(self.dispatcher, operation, arguments, self)
        setattr(self, slot, worker)
        def replied(result):
            if not self.closing and self.session == arguments['session'] and callback:
                callback(result)
        def failed(message):
            if not self.closing:
                self.error.emit(message)
            if failure:
                failure()
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
                and not self.transcribe_worker and not self.settled):
            self.settled = True
            self.stopped.emit()

    def view(self, **params):
        self.params = params
        self.poll_at = 0
        self._tick()

    def _enqueue(self, operation, arguments, callback=None, *, first=False):
        if len(self.queue) >= 12:
            self.error.emit('操作正在处理，请稍后再试')
            return
        item = (operation, arguments, callback)
        self.queue.appendleft(item) if first else self.queue.append(item)
        self._pump()

    def _pump(self):
        if self.worker or not self.queue:
            return
        operation, arguments, callback = self.queue.popleft()
        epoch = self._connection_epoch
        worker = LiveRequest(self.dispatcher, operation, arguments, self)
        self.worker = worker
        def replied(result):
            if epoch != self._connection_epoch:
                return
            if operation in {'heartbeat', 'stop'}:
                self.active = bool(result['active'])
                self.activity_changed.emit()
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
                return
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
            self._pump()
            self._finish_stop()
        worker.finished.connect(finished)
        worker.start()

    def _tick(self):
        if not self.connected or self.settled or self.core_stopped:
            return
        now = time.monotonic()
        operations = {item[0] for item in self.queue}
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
        self.queue.clear()
        if not self.session:
            # Open may still be in flight. Its callback starts the stop timer.
            if not self.worker:
                self.settled = True
                self.timer.stop()
                return True
            return False
        self.timer.start()
        self._tick()
        return False
