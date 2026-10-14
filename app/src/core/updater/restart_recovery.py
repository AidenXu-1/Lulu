"""Recover abandoned installs before GUI/Core startup, outside the running App."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from .constants import default_updates_root
from .install import InstallTransaction, _same_identity
from .helper_protocol import read_failure, fail_before_swap, independent_environment
from .models import UpdateError
from .process_identity import current_identity, matches_process
from .secure_io import read_nofollow
from .state import StateStore


@contextmanager
def helper_guard(root, *, wait_seconds=0):
    """The live OS lock, unlike update.lock, is released on process death."""
    path = Path(root) / 'transactions/helper.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise UpdateError('unsafe_lock', '更新恢复锁不安全')
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(.05)
        yield True
    finally:
        os.close(fd)


def running_app(target, *, exclude_pid=None):
    executable = str(Path(target) / 'Contents/MacOS/Lulu')
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError('process_check', '无法核对运行中的 Lulu，保留原程序') from exc
    if result.returncode or not result.stdout.strip():
        raise UpdateError('process_check', '无法核对运行中的 Lulu，保留原程序')
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise UpdateError('process_check', '程序列表无法读取，保留原程序')
        if parts[0] != str(exclude_pid):
            command = parts[1]
            if command == executable:
                # Recovery itself owns helper_guard and is deliberately excluded.
                return True
    return False


def save_result(root, result, *, error_code=''):
    StateStore(root).record_result(result, error_code=error_code)


def _wait_other_startups(target):
    """Keep the guard while competing pre-GUI entries time out and exit.

    Their guard wait is two seconds. Releasing our guard or continuing into
    Core here can strand recovery when two ordinary launches race.
    """
    deadline = time.monotonic() + 3
    while running_app(target, exclude_pid=os.getpid()):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)
    return True


def launch_pending_recovery(*, root=None, executable=None):
    """Return true when an installer owns startup; the caller must exit.

    A normal double-click must not open another old GUI during copying or
    exchange. Briefly wait for a finishing helper so its ordinary rollback
    relaunch can continue after the transaction lock has been removed.
    """
    root = Path(root or default_updates_root())
    if not os.path.lexists(root / 'transactions/update.lock'):
        return False
    tx = InstallTransaction(root)
    with helper_guard(root, wait_seconds=2) as acquired:
        if not acquired:
            return True
        # A live helper may have completed while we waited for its OS lock.
        # Its successful cleanup is not a corrupt or abandoned transaction.
        if not os.path.lexists(tx.lock):
            return False
        try:
            txn = read_nofollow(tx.lock, mode=0o600, maximum=65).decode('ascii').strip()
            if len(txn) != 64 or any(c not in '0123456789abcdef' for c in txn):
                raise UpdateError('txn', '更新恢复编号无效')
            path = tx.transactions / txn / 'install_plan.json'
            plan = tx.load_plan(path, validate_location=False)
        except (UpdateError, OSError, UnicodeError):
            save_result(root, 'failed_state_corrupt')
            # Invalid input is not authority to remove a transaction lock.
            raise UpdateError('plan', '更新恢复记录无法验证，请手动重新安装；原资料保留')
        failure = read_failure(path, plan)
        if failure is not None:
            if (not _same_identity(Path(plan['target_app']), plan['target_lstat'])
                    or any(os.path.lexists(Path(plan[k]).parent) for k in ('swap_new','backup_app'))):
                raise UpdateError('helper_result', '安装结果与现有程序不一致，已保留恢复记录')
            fail_before_swap(path, plan, tx, failure['error_code'], failure['exit_code'])
            return False
        if matches_process(plan['old_pid'], plan['old_pid_start_time'], plan['old_executable_path']):
            return True
        if not _wait_other_startups(plan['target_app']):
            return True
        executable = Path(executable or sys.executable)
        if executable.is_symlink():
            raise UpdateError('recovery_source', '更新尚未完成，请从应用程序文件夹打开 Lulu；原资料保留')
        executable = executable.resolve(strict=True)
        if executable != Path(plan['target_app']) / 'Contents/MacOS/Lulu':
            raise UpdateError('recovery_source', '更新尚未完成，请从应用程序文件夹打开 Lulu；原资料保留')
        started, actual = current_identity()
        subprocess.Popen([str(executable), '--lulu-update-recover', str(path), str(os.getpid()), started, actual],
                         close_fds=True, start_new_session=True, env=independent_environment())
        return True


def run_recovery(plan_path, parent_pid, parent_started, parent_executable):
    try:
        return _run_recovery(plan_path, parent_pid, parent_started, parent_executable)
    except (UpdateError, OSError) as exc:
        # Preserve recovery even when its result cannot be written. The entry
        # point presents the repair window without starting the business Core.
        try:
            save_result(Path(plan_path).resolve().parents[2], 'failed', error_code=getattr(exc, 'code', 'recovery_io'))
        except (UpdateError, OSError):
            pass
        return 5


def _run_recovery(plan_path, parent_pid, parent_started, parent_executable):
    path = Path(plan_path).resolve()
    root = path.parents[2]
    tx = InstallTransaction(root)
    # The dispatcher holds the guard until Popen returns. A fast child can reach
    # here first; briefly wait instead of silently abandoning the recovery.
    with helper_guard(root, wait_seconds=2) as acquired:
        if not acquired:
            return 3
        plan = tx.load_plan(path, validate_location=False)
        deadline = time.monotonic() + 30
        while matches_process(int(parent_pid), parent_started, parent_executable):
            if time.monotonic() >= deadline:
                return 4
            time.sleep(.1)
        if not _wait_other_startups(plan['target_app']):
            return 4
        from .helper_entry import stop_update_core
        try:
            stop_update_core(plan)
        except Exception:
            save_result(root, 'failed', error_code='update_child_still_running')
            return 5
        result = tx.recover(path)
        save_result(root, result)
        if result in {'rolled_back', 'installed'}:
            subprocess.Popen([str(Path(plan['target_app']) / 'Contents/MacOS/Lulu')],
                             close_fds=True, start_new_session=True, env=independent_environment())
        return 0 if result in {'rolled_back', 'installed'} else 5
