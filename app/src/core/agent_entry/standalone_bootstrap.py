"""Prepare through the signed Core while the main window is already visible."""
from pathlib import Path
import sys
from ..updater.constants import AUTHORIZATION_WAIT_FLAG, PREPARATION_TIMEOUT_SECONDS


_PREPARATION_FAILURES = {
    'JOB_CORE_ACCESS_INVALID', 'JOB_CORE_PARTITION_INVALID',
    'PRODUCT_TASK_HANDOVER_INVALID',
    'LOCAL_INTEGRITY_RECOVERY_REQUIRED', 'LOCAL_INTEGRITY_FILE_UNSAFE', 'LOCAL_INTEGRITY_BUSY',
}


def preparation_failure_code(output):
    """Only closed codes from this signed child; no stale diagnostics or text."""
    try:
        lines = bytes(output)[-4096:].splitlines()
    except (TypeError, ValueError):
        return 'INSTALLATION_PREPARATION_FAILED'
    codes = [line.removeprefix(b'LULU_PREPARATION_FAILURE:').decode('ascii', errors='replace')
             for line in lines if line.startswith(b'LULU_PREPARATION_FAILURE:')]
    return codes[0] if len(codes) == 1 and codes[0] in _PREPARATION_FAILURES else 'INSTALLATION_PREPARATION_FAILED'


def preparation_failure_message(reason):
    if isinstance(reason, str) and reason.startswith('LOCAL_INTEGRITY_'):
        return ('本地任务存储未能通过检查，原资料已保留。请先重开 Lulu；'
                '若仍无法使用，请联系开发者检查存储，勿删除任务目录。错误：'+reason)
    if reason in {'JOB_CORE_ACCESS_INVALID', 'JOB_CORE_PARTITION_INVALID'}:
        return ('旧任务的安全凭据校验未通过，任务功能暂不可用。文稿、音频和原记录已保留。'
                '请联系开发者核对升级记录，勿删除钥匙串条目。错误：'+reason)
    if reason == 'PRODUCT_TASK_HANDOVER_INVALID':
        return ('旧任务交接校验未通过，任务功能暂不可用。文稿、音频和原记录已保留。'
                '请联系开发者核对升级记录，勿删除或移走任务目录。错误：'+reason)
    if reason == 'INSTALLATION_PREPARATION_TIMEOUT':
        return ('启动准备等待超时，暂时无法开始任务。已有记录已保留。'
                '请重新打开 Lulu；若仍无法使用，请联系开发者检查。')
    return ('Lulu 启动准备未完成，暂时无法开始任务。请重新打开 Lulu；'
            '若仍无法使用，请联系开发者检查。已有记录已保留。')


def start_preparation(application, on_ready, *, on_start, on_failed):
    """Keep installation checks in the background, with no preparation window."""
    from .standalone_source import DESCRIPTOR
    app = Path(sys.executable).parent.parent.parent
    if not getattr(sys, 'frozen', False) or not (app/DESCRIPTOR).exists():
        return False
    from PySide6.QtCore import QProcess, QTimer
    from PySide6.QtWidgets import QMessageBox
    from .standalone_paths import installed_app
    try:
        installed_app(app)
    except Exception:
        QMessageBox.information(None, '安装 Lulu', '请先把 Lulu 拖入“应用程序”文件夹，再从那里打开。')
        QTimer.singleShot(0, application.quit)
        return True
    process = QProcess(application)
    process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)
    arguments = ['--prepare-install']
    updating = '--lulu-update-ready' in sys.argv
    if updating:
        index = sys.argv.index('--lulu-update-ready')
        if len(sys.argv) <= index+1:
            QTimer.singleShot(0, application.quit)
            return True
        arguments += ['--update-plan', sys.argv[index+1]]
    finished = False
    application._lulu_install_preparation_failure = ''
    timer = QTimer(application); timer.setSingleShot(True)
    def stop_process():
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill(); process.waitForFinished(3000)
    def cancel():
        nonlocal finished
        finished = True; timer.stop(); stop_process()
    def complete(code, reason=None):
        nonlocal finished
        if finished:
            return
        finished = True; timer.stop()
        if code == 0:
            on_ready()
        else:
            application._lulu_install_preparation_failure = (
                reason or preparation_failure_code(process.readAllStandardOutput()))
            stop_process()
            on_failed()
            if updating:
                application.quit()
    process.finished.connect(lambda code, _status: complete(code))
    process.errorOccurred.connect(lambda _error: complete(1, 'INSTALLATION_PREPARATION_FAILED'))
    timer.timeout.connect(lambda: complete(1, 'INSTALLATION_PREPARATION_TIMEOUT'))
    application.aboutToQuit.connect(cancel)
    application._lulu_install_preparation = (process, timer)
    # The task connection stays gated until preparation has succeeded.
    on_start()
    # An old helper cannot wait longer; finish/stop our preparation before its
    # fixed rollback deadline. New helpers explicitly advertise the new budget.
    legacy_helper = updating and AUTHORIZATION_WAIT_FLAG not in sys.argv
    timer.start(35000 if legacy_helper else PREPARATION_TIMEOUT_SECONDS * 1000)
    process.start(str(app/'Contents/MacOS/lulu-agent-core'), arguments)
    return True
