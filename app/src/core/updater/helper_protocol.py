"""Bound handoff receipts. A spawned process is not an accepted installation."""
import os
from pathlib import Path

from .models import UpdateError
from .process_identity import current_identity, matches_process
from .secure_io import atomic_json, load_json
from .state import StateStore

_BINDING = {'txn', 'nonce', 'plan_sha256'}
_ACCEPTED = _BINDING | {'schema', 'pid', 'pid_start_time', 'executable_path'}
_RESULT = _BINDING | {'schema', 'phase', 'error_code', 'exit_code'}


def _binding(plan):
    return {key: plan[key] for key in _BINDING}


def accept(path, plan, transaction):
    if not transaction.owns(plan['txn']):
        raise UpdateError('locked', '更新事务已变化，保留当前程序')
    receipt = Path(path).parent / 'helper-accepted.json'
    if os.path.lexists(receipt):
        raise UpdateError('helper_replay', '安装接管记录已存在，保留当前程序')
    started, executable = current_identity()
    if Path(executable) != Path(plan['target_app'])/'Contents/MacOS/Lulu':
        raise UpdateError('helper_identity', '安装助手身份不符，保留当前程序')
    atomic_json(receipt, dict(schema=1, **_binding(plan), pid=os.getpid(),
                             pid_start_time=started, executable_path=executable))


def accepted(path, plan, child_pid):
    receipt = Path(path).parent / 'helper-accepted.json'
    if not os.path.lexists(receipt):
        return False
    value = load_json(receipt, allowed=_ACCEPTED, required=_ACCEPTED)
    if (value['schema'] != 1 or any(value[k] != plan[k] for k in _BINDING)
            or type(value['pid']) is not int or value['pid'] != child_pid
            or not isinstance(value['pid_start_time'], str)
            or value['executable_path'] != str(Path(plan['target_app'])/'Contents/MacOS/Lulu')):
        return False
    return matches_process(child_pid, value['pid_start_time'], value['executable_path'])


def read_failure(path, plan):
    receipt = Path(path).parent / 'launch-result.json'
    if not os.path.lexists(receipt):
        return None
    value = load_json(receipt, allowed=_RESULT, required=_RESULT)
    if (value['schema'] != 1 or value['phase'] != 'before_swap'
            or any(value[k] != plan[k] for k in _BINDING)
            or type(value['exit_code']) is not int or not isinstance(value['error_code'], str)):
        raise UpdateError('helper_result', '安装结果无法验证，保留当前程序')
    return value


def fail_before_swap(path, plan, transaction, error_code, exit_code):
    """Called with a validated plan and no exchange, while owning this txn.

    Keep the lock if either durable write fails: next startup can recover the
    bound receipt. Never release another transaction's lock.
    """
    if not transaction.owns(plan['txn']):
        raise UpdateError('locked', '更新事务已变化，保留当前程序')
    atomic_json(Path(path).parent/'launch-result.json',
                dict(schema=1, **_binding(plan), phase='before_swap',
                     error_code=error_code, exit_code=exit_code))
    store = StateStore(transaction.root)
    snapshot = store.load()
    snapshot.txn = plan['txn']
    snapshot.state, snapshot.ui_state = 'failed', 'preparation_error'
    snapshot.error_code = error_code
    snapshot.message = '安装尚未开始，原版本和资料已保留。请重新检查更新后重试。'
    store.save(snapshot)
    transaction.release(expected_txn=plan['txn'])


def independent_environment():
    """PyInstaller requires a new runtime for a child outliving its parent."""
    return {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}
