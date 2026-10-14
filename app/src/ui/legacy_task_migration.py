"""One-time background format compatibility, with an exception-only settings exit."""
from pathlib import Path
import sys
from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from .control_foundation import adopt_button


def migration_result(output):
    try:
        results = [line.removeprefix(b'LULU_MIGRATION_RESULT:')
            for line in bytes(output)[-4096:].splitlines() if line.startswith(b'LULU_MIGRATION_RESULT:')]
    except (TypeError, ValueError):
        results = []
    return results[0].decode('ascii') if results in ([b'complete'], [b'partial']) else None


def failure_message(output):
    try:
        codes = [line.removeprefix(b'LULU_MIGRATION_FAILURE:').decode('ascii', errors='replace')
            for line in bytes(output)[-4096:].splitlines() if line.startswith(b'LULU_MIGRATION_FAILURE:')]
    except (TypeError, ValueError):
        codes = []
    code = codes[0] if len(codes) == 1 else None
    explanations = {
        'PRODUCT_UNFINISHED_TASKS': '还有任务在运行，请先结束任务。',
        'PRODUCT_TASKS_CHANGED': '旧记录在检查期间发生变化，请结束旧版任务后重试。',
        'PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS': '旧安装记录来源不一致，请联系开发者核查。',
        'PRODUCT_PREVIOUS_CORE_REQUIRED': '有旧版本记录暂时无法核验，请联系开发者核查。',
        'JOB_IMPORT_CONFLICT': '旧记录与当前任务有冲突，请联系开发者核查。',
        'JOB_CORE_KEY_MISSING': '旧任务校验材料缺失，请联系开发者核查。',
        'JOB_LEDGER_HMAC_INVALID': '旧任务记录未通过完整性检查，请联系开发者核查。',
        'JOB_LEDGER_HIGH_WATER_ROLLBACK': '旧记录与保存的校验信息不一致，原文件保留，请联系开发者核查。',
        'JOB_CORE_ACCESS_INVALID': '旧校验材料的程序身份未通过检查，原文件保留，请联系开发者核查。',
        'JOB_BINDING_FILE_CHANGED': '旧任务存储信息发生变化，请联系开发者核查。',
        'JOB_LEDGER_HIGH_WATER_WRITE_UNCERTAIN': '保存被中断，请重开 Lulu 后重试检查。',
        'LOCAL_INTEGRITY_BUSY': '本地资料正在被使用，请稍后重试。',
    }
    return '部分旧版任务尚未读取。'+explanations.get(code,
        '读取未完成，可能是授权取消或检查中断。')+'原资料和新任务已保留，不会自动重复请求授权。'


class LegacyTaskInheritanceController(QObject):
    changed = Signal()
    finished = Signal(bool)

    def __init__(self, *, app, product, busy_probe, parent=None):
        super().__init__(parent)
        self.app, self.product, self.busy_probe = Path(app), Path(product), busy_probe
        self.process = None
        self.active = False
        self._auto_checked = False
        self._closing = False
        self.error = ''
        from ..core.agent_entry.local_task_storage import migration_status
        try: self.state = migration_status(product)
        except Exception: self.state = 'failed'

    def maybe_start(self):
        if self._closing or self.active or self._auto_checked: return
        self._auto_checked = True
        from ..core.agent_entry.local_task_storage import automatic_migration_needed
        try:
            needed = automatic_migration_needed(self.product)
        except Exception:
            self.state = 'failed'
            self.error = '本地兼容记录无法读取，原资料保留。请联系开发者核查。'
            self.changed.emit()
            return
        if needed: self._start(automatic=True)

    def retry(self):
        if self._closing or self.active: return
        if self.busy_probe():
            self.error = '请先结束当前任务，再重试资料检查。原资料仍在原处。'
            self.changed.emit()
            return
        self._start(automatic=False)

    def _start(self, *, automatic):
        self.active = True
        self.error = ''
        self.changed.emit()
        process = self.process = QProcess(self)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.finished.connect(lambda code, status: self._done(code == 0 and status == QProcess.NormalExit, process))
        process.errorOccurred.connect(lambda error: self._done(False, process) if error in
            {QProcess.FailedToStart, QProcess.Crashed} else None)
        process.start(str(self.app/'Contents/MacOS/lulu-agent-core'),
            ['--inherit-legacy-tasks' if automatic else '--migrate-legacy-tasks'])

    def _done(self, success, process):
        if not self.active or process is not self.process: return
        output = process.readAllStandardOutput()
        result = migration_result(output) if success else None
        self.state = result or 'failed'
        self.error = '' if result else failure_message(output)
        self.active = False
        self.process = None
        process.deleteLater()
        self.changed.emit()
        if not self._closing:
            # Even an interrupted item-level commit requires a fresh Core read.
            self.finished.emit(result is not None)

    def shutdown(self):
        self._closing = True
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)


class LegacyTaskMigrationPanel(QWidget):
    """Hidden during ordinary operation; only explains a compatibility exception."""
    def __init__(self, *, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        status_font = self.status.font(); status_font.setBold(True); self.status.setFont(status_font)
        layout.addWidget(self.status)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName('pageMeta')
        layout.addWidget(self.note)
        self.button = QPushButton('重试资料检查')
        adopt_button(self.button, role='secondary', accessible_name='重试未完成的旧版资料检查')
        self.button.clicked.connect(controller.retry)
        layout.addWidget(self.button)
        controller.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        controller = self.controller
        # A completed upgrade has no permanent recovery feature or success banner.
        self.setVisible(controller.active or controller.state in {'failed', 'partial'})
        self.button.setVisible(not controller.active and controller.state == 'failed')
        if controller.active:
            self.status.setText('正在兼容旧版资料')
            self.note.setText('Lulu 正在自动检查旧版记录。若系统要求授权，请亲自处理；原资料保留，新任务可以照常使用。')
        elif controller.state == 'partial':
            self.status.setText('部分旧记录尚未合并')
            self.note.setText('已核验且没有冲突的任务已继承。未核验或存在冲突的旧原件完整保留，现有任务不会被覆盖；旧任务不会自动重跑。')
        elif controller.state == 'failed':
            self.status.setText('旧版资料检查未完成')
            self.note.setText(controller.error or failure_message(b''))


def installed_inheritance_controller(*, busy_probe, parent):
    if not getattr(sys, 'frozen', False): return None
    app = Path(sys.executable).parent.parent.parent
    from ..core.agent_entry.standalone_source import uses_local_storage
    if not uses_local_storage(app): return None
    return LegacyTaskInheritanceController(app=app, product=Path.home()/'Library/Application Support/Lulu',
        busy_probe=busy_probe, parent=parent)
