"""GUI-side installation handoff; preparation runs off the UI thread."""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
import time

from .helper_protocol import accepted, fail_before_swap, independent_environment, read_failure
from .install import InstallTransaction, ensure_install_space
from .models import UpdateError
from .process_identity import current_identity
from .state import StateStore
from .manifest import version_tuple, build_tuple
from ...version import __version__, __build__


@dataclass
class InstallLaunch:
    transaction: InstallTransaction
    path: Path
    plan: dict
    child: object
    deadline: float
    stopping_at: float | None = None
    failure_code: str = ''

    def poll(self):
        """Poll without sleeping or synchronously waiting for the child."""
        now = time.monotonic()
        code = self.child.poll()
        if code is not None:
            from .restart_recovery import helper_guard
            with helper_guard(self.transaction.root) as acquired:
                if not acquired:
                    return 'waiting'  # Another live installer still owns startup.
                failure = read_failure(self.path, self.plan)
                if self.transaction.owns(self.plan['txn']):
                    fail_before_swap(self.path, self.plan, self.transaction,
                                     failure['error_code'] if failure else self.failure_code or 'helper_exit', code)
            return 'failed'
        if self.stopping_at is not None:
            if now - self.stopping_at > 5:
                self.child.kill()
            return 'waiting'
        try:
            if accepted(self.path, self.plan, self.child.pid):
                return 'accepted'
            if now < self.deadline:
                return 'waiting'
            self.failure_code = 'helper_accept_timeout'
        except (OSError, UpdateError):
            self.failure_code = 'helper_accept_invalid'
        self.child.terminate()
        self.stopping_at = now
        return 'waiting'


def prepare_launch(executable, snapshot, root, *, cancelled=lambda: False):
    try:
        target_version = (version_tuple(snapshot.latest_version), build_tuple(snapshot.latest_build))
        installed_version = (version_tuple(__version__), build_tuple(__build__))
    except UpdateError as exc:
        raise UpdateError('version_recheck', '更新版本信息不完整，请重新检查当前版本') from exc
    if target_version <= installed_version:
        raise UpdateError('not_newer', '此安装包不高于当前版本，请重新检查更新')
    raw = Path(executable)
    if raw.is_symlink():
        raise UpdateError('helper_identity', '更新安装程序无法验证')
    executable = raw.resolve()
    if (len(executable.parents) < 3 or executable.name != 'Lulu'
            or executable.parents[1].name != 'Contents' or executable.parents[2].name != 'Lulu.app'):
        raise UpdateError('location', '请从应用程序文件夹打开 Lulu 后更新')
    target = executable.parents[2]
    seal = subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(target)],
                          capture_output=True,timeout=30,check=False)
    if not stat.S_ISREG(executable.lstat().st_mode) or seal.returncode:
        raise UpdateError('helper_identity', '更新安装程序无法验证')
    staged = Path(snapshot.staged_app)
    if not staged.is_dir() or len(snapshot.staged_manifest_sha256) != 64:
        raise UpdateError('staged', '安装准备不完整，请重新检查更新')
    ensure_install_space(staged, target.parent)
    if cancelled():
        raise UpdateError('cancelled', '安装准备已取消，原版本保留')
    tx = InstallTransaction(root)
    started, old_executable = current_identity()
    path, plan = tx.create_plan(target=target, staged_app=staged,
        version=snapshot.latest_version, build=snapshot.latest_build, old_pid=os.getpid(),
        old_pid_start_time=started, old_executable_path=old_executable,
        staged_sha=snapshot.staged_manifest_sha256, launch_protocol=True)
    # The helper may finish immediately: no GUI write of "installing" after spawn.
    snapshot.txn = plan['txn']
    snapshot.state, snapshot.ui_state = 'installing', 'preparing'
    snapshot.error_code, snapshot.message = '', '正在准备安装，确认接管后将自动重新打开。'
    try:
        StateStore(root).save(snapshot)
        if cancelled():
            raise UpdateError('cancelled', '安装准备已取消，原版本保留')
        child = subprocess.Popen([str(executable),'--lulu-update-helper',str(path)],
                                 close_fds=True,start_new_session=True,env=independent_environment())
    except Exception as exc:
        fail_before_swap(path, plan, tx, getattr(exc, 'code', 'helper_launch'), 2)
        raise UpdateError(getattr(exc, 'code', 'helper_launch'), '安装未开始，原版本和资料已保留') from exc
    return InstallLaunch(tx, path, plan, child, time.monotonic()+30)
