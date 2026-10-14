"""Authenticated pre-swap export, with a process-wide business-write barrier.

The lock is held by the old helper. Core writers hold shared locks for their
entire request; only the two public history export reads may cross the barrier.
No task key is exported and this module never edits Keychain access rules.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .constants import default_updates_root, PREPARATION_TIMEOUT_SECONDS
from .models import UpdateError
from .secure_io import ensure_private_dir, read_nofollow


def _export_read(request):
    if type(request) is not dict or set(request) != {'command', 'arguments'}:
        return False
    args = request.get('arguments')
    if request.get('command') != 'argv' or type(args) is not dict or set(args) != {'argv'}:
        return False
    argv = args['argv']
    return (argv == ['jobs', 'list', '--output', 'json'] or
        (type(argv) is list and len(argv) == 6 and argv[:3] == ['jobs', 'receipt', '--job-id']
         and type(argv[3]) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', argv[3])
         and argv[4:] == ['--output', 'json']))


@contextmanager
def handover_lock(root=None, *, exclusive=False):
    directory = ensure_private_dir(Path(root or default_updates_root())/'transactions')
    path = directory/'handover.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
            raise UpdateError('handover_lock', '任务交接锁无法验证，当前程序保留')
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError('handover_busy', '正在保存或交接任务，请稍后重试') from exc
        yield fd
    finally:
        os.close(fd)


@contextmanager
def business_request_guard(request, *, root=None):
    root = Path(root or default_updates_root())
    guard = handover_lock(root)
    try:
        guard.__enter__()
    except UpdateError as exc:
        # Export is a short phase. Once its child exits, even history reads
        # must stop entering, so no client can revive the old service at swap.
        if (exc.code != 'handover_busy' or not _export_read(request)
                or read_nofollow(root/'transactions/handover.lock',
                                 mode=0o600, maximum=32) != b'export\n'):
            raise
        yield
    else:
        try:
            yield
        finally:
            guard.__exit__(None, None, None)


def _phase(fd, value):
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    if os.write(fd, value) != len(value):
        raise OSError('handover phase write incomplete')
    os.fsync(fd)


def checkpoint_entry(arguments):
    """Called only by the signed old Core, while still at its original path."""
    from ..agent_entry.standalone_paths import frozen_app, state_for_app
    from ..agent_entry.history_checkpoint import prepare_checkpoint, read_checkpoint
    from ..agent_entry.ownership import verify_ownership_receipt
    from ..agent_entry.installed_materials import _private_json
    from ..agent_entry.standalone_source import core_cdhash
    from ..cli_runtime.core_task_keychain import CoreTaskKeyringStore
    from .install import InstallTransaction
    from .process_identity import process_identity
    app = frozen_app('lulu-agent-core')
    if len(arguments) != 1:
        return 2
    path = Path(arguments[0])
    root = default_updates_root()
    # If the owning helper dies, this export must not remain alive while a
    # later recovery operation reopens or replaces the installed App.
    import threading
    import time
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(.2)
            if os.getppid() != parent:
                os._exit(1)
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        plan = InstallTransaction(root).load_plan(path)
        if (Path(plan['target_app']) != app or process_identity(plan['old_pid']) is not None
                or read_nofollow(root/'transactions/update.lock', maximum=128).decode().strip() != plan['txn']):
            raise RuntimeError('PRODUCT_TASKS_CHANGED')
        # A successful shared acquisition means no helper has frozen writers.
        try:
            with handover_lock(root):
                pass
        except UpdateError as exc:
            if exc.code != 'handover_busy':
                raise
        else:
            raise RuntimeError('PRODUCT_TASKS_CHANGED')
        from ..agent_entry.standalone_source import uses_local_storage
        from ..local_integrity import product_local_store
        keys = product_local_store(root.parent) if uses_local_storage(app) else CoreTaskKeyringStore()
        checkpoint = prepare_checkpoint(app, keys=keys)
        state = state_for_app(app)
        owner = verify_ownership_receipt(state/'ownership.json', key_store=keys)
        generation = core_cdhash(app)
        product = root.parent
        if read_checkpoint(product_root=product, owner=owner, generation=generation,
                           keys=keys, state=state) is None:
            raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
        body = _private_json(checkpoint)['body']
        proof = {'schema': 1, 'manifest': owner['component_manifest_sha256'],
                 'generation': generation, 'state': str(state),
                 'checkpoint_sha256': hashlib.sha256(read_nofollow(checkpoint)).hexdigest(),
                 'snapshot': body['snapshot'], 'handover_snapshot': body['handover_snapshot']}
        print(json.dumps(proof, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        from ..diagnostics import record_failure
        record_failure('update_handover', exc, root=root.parent/'AgentEntry')
        return 1


def _assert_old_core_stopped(app):
    result = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,comm='],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or not result.stdout.strip():
        raise UpdateError('handover_process_check', '无法确认旧后台已退出，当前程序保留')
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise UpdateError('handover_process_check', '无法确认旧后台已退出，当前程序保留')
        if parts[1] == str(app/'Contents/MacOS/lulu-agent-core'):
            raise UpdateError('handover_core_running', '旧后台尚未退出，当前程序保留')


class PreSwapHandover:
    def __init__(self, path, plan):
        self.path, self.plan = Path(path), plan
        self.root = self.path.parents[2]
        self.app = Path(plan['target_app'])
        self.guard = None
        self.guard_fd = None
        self.service = None
        self.plist = None
        self.stopped = False

    def release(self):
        if self.guard is not None:
            guard, self.guard = self.guard, None
            try:
                _phase(self.guard_fd, b'')
            finally:
                self.guard_fd = None
                guard.__exit__(None, None, None)

    def prepare(self):
        from ..agent_entry.standalone_paths import state_for_app, service_for_app
        from ..agent_entry.effects import CurrentUserLaunchctlService
        from ..agent_entry.product_transaction import snapshot_digest
        from ..agent_entry.installed_materials import MATERIALS_DIR
        from ..agent_entry.task_handover import FILENAME
        from .restart_recovery import running_app
        if running_app(self.app, exclude_pid=os.getpid()):
            raise UpdateError('handover_busy', '仍有 Lulu 窗口运行，当前程序保留')
        self.guard = handover_lock(self.root, exclusive=True)
        try:
            self.guard_fd = self.guard.__enter__()
        except BaseException:
            self.guard = None
            raise
        state = state_for_app(self.app)
        self.plist = state/'core.plist'
        self.service = CurrentUserLaunchctlService(label=service_for_app(self.app))
        _phase(self.guard_fd, b'export\n')
        try:
            result = subprocess.run([str(self.app/'Contents/MacOS/lulu-agent-core'),
                '--prepare-update-checkpoint', str(self.path)], capture_output=True,
                timeout=PREPARATION_TIMEOUT_SECONDS, check=False)
        finally:
            _phase(self.guard_fd, b'frozen\n')
        if result.returncode or len(result.stdout) > 8192:
            raise UpdateError('handover_failed', '旧任务未能完成交接，当前程序保留')
        proof = json.loads(result.stdout)
        fields = {'schema', 'manifest', 'generation', 'state', 'checkpoint_sha256', 'snapshot', 'handover_snapshot'}
        if (type(proof) is not dict or set(proof) != fields or type(proof['schema']) is not int or proof['schema'] != 1
                or proof['state'] != str(state)
                or (proof['handover_snapshot'] is not None and
                    (type(proof['handover_snapshot']) is not str or
                     re.fullmatch(r'[a-f0-9]{64}', proof['handover_snapshot']) is None))
                or any(type(proof[k]) is not str or re.fullmatch(pattern, proof[k]) is None
                       for k, pattern in [('manifest', r'[a-f0-9]{64}'), ('generation', r'[a-f0-9]{40}'),
                                          ('checkpoint_sha256', r'[a-f0-9]{64}'), ('snapshot', r'[a-f0-9]{64}')])):
            raise UpdateError('handover_failed', '旧任务交接回执无法验证，当前程序保留')
        checkpoint = self.root.parent/'AgentEntry/history-checkpoints'/(proof['manifest']+'.json')
        # The export rejects running/queued work before any stop. A concurrent
        # writer cannot enter after that check while our exclusive lock is held.
        if running_app(self.app, exclude_pid=os.getpid()):
            raise UpdateError('handover_busy', 'Lulu 已重新打开，当前程序保留')
        self.stopped = True
        self.service.terminate_registered_process()
        self.service.stop()
        _assert_old_core_stopped(self.app)
        from ..agent_entry.standalone_source import uses_local_storage
        from ..local_integrity import TASK_DIRECTORY
        task_root = (self.root.parent/TASK_DIRECTORY if uses_local_storage(self.app)
                     else self.root.parent/'task-generations'/proof['generation'])
        if (hashlib.sha256(read_nofollow(checkpoint)).hexdigest() != proof['checkpoint_sha256']
                or snapshot_digest(task_root) != proof['snapshot']
                or snapshot_digest(state/MATERIALS_DIR/FILENAME) != proof['handover_snapshot']):
            raise UpdateError('handover_changed', '任务在交接期间发生变化，当前程序保留')

    def restore(self):
        self.release()
        if self.stopped and self.service is not None and not self.service.is_loaded():
            self.service.start(self.plist)
