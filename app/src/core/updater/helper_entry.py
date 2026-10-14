from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

from .cache_cleanup import cleanup_successful_update, record_cleanup_failure
from .install import InstallTransaction, RollbackBlocked, ensure_install_space
from .process_identity import matches_process, process_identity, same_identity
from .state import StateStore
from .models import UpdateError
from .constants import AUTHORIZATION_WAIT_FLAG
from .handover import PreSwapHandover
from .helper_protocol import accept, fail_before_swap, independent_environment


def stop_update_child(child) -> None:
    """Only stop the process we launched, and observe exit before replacing it."""
    if child is None:
        return
    try:
        if child.poll() is not None:
            return
        child.terminate()
        child.wait(timeout=5)
    except Exception as exc:
        raise RollbackBlocked("新版尚未退出，保留新旧程序供恢复") from exc


def stop_update_core(plan) -> None:
    target = plan.get('target_app')
    if type(target) is not str:
        return
    from ..agent_entry.standalone_source import DESCRIPTOR
    if not (Path(target)/DESCRIPTOR).exists():
        return
    try:
        from ..agent_entry.standalone_paths import service_for_app
        from ..agent_entry.effects import CurrentUserLaunchctlService
        service = CurrentUserLaunchctlService(label=service_for_app(Path(target)))
        service.terminate_registered_process()
        service.stop()
    except Exception as exc:
        raise RollbackBlocked('新版后台尚未退出，保留新旧程序供恢复') from exc


def wait_for_child_ready(transaction, ready_path, plan, child, identity, *, timeout):
    deadline = time.monotonic() + timeout
    observed_at = None
    while True:
        if child.poll() is not None:
            return False
        now = time.monotonic()
        if observed_at is None and now >= deadline:
            break
        if ready_path.exists() or ready_path.is_symlink():
            if not transaction.validate_ready(
                ready_path, plan, pid_validator=matches_process, expected_child=identity,
            ):
                break
            if observed_at is None:
                observed_at = now
            # Keep the previous App until a ready child survives a short watch.
            # This extends only the new helper; old helpers keep their protocol.
            if now - observed_at >= 8:
                return True
        elif observed_at is not None:
            break
        time.sleep(0.1)
    stop_update_child(child)
    return False


def run_helper(plan_path: str) -> int:
    from .restart_recovery import helper_guard
    path = Path(plan_path).resolve()
    with helper_guard(path.parents[2]) as acquired:
        if not acquired:
            return 3
        try:
            return _run_helper(plan_path)
        except Exception as exc:
            # Preserve the lock when a plan or durable result cannot be trusted.
            # A later normal start can inspect/recover it under helper_guard.
            from ..diagnostics import record_failure
            record_failure('update_helper', exc, root=path.parents[2].parent/'AgentEntry')
            # If copying has not exchanged the original App, normal startup can
            # recover this retained transaction or present the repair window.
            # Do not leave a user with every window closed after a caught error.
            _reopen_untouched_app(path)
            return 5


def _reopen_untouched_app(path):
    from .install import _same_identity
    try:
        tx = InstallTransaction(path.parents[2])
        plan = tx.load_plan(path)
        if (tx.owns(plan['txn']) and process_identity(plan['old_pid']) is None
                and _same_identity(Path(plan['target_app']), plan['target_lstat'])):
            subprocess.Popen([str(Path(plan['target_app'])/'Contents/MacOS/Lulu')],
                close_fds=True, start_new_session=True, env=independent_environment())
    except Exception:
        pass  # Keep the owned lock and original App for the next normal open.


def _abort_before_swap(transaction, updates_root, plan, handover, error):
    """Restore the old service without interpreting an untouched App as a swap."""
    code = error.code if isinstance(error, UpdateError) else 'handover_failed'
    if not code.startswith('handover_'):
        code = 'handover_' + code
    try:
        handover.restore()
    except Exception:
        code = 'handover_restore_failed'
    receipt_error = None
    try:
        fail_before_swap(Path(plan['ready_path']).parent/'install_plan.json',
                         plan, transaction, code, 5)
    except Exception as exc:
        receipt_error = exc
    finally:
        handover.release()
    try:
        subprocess.Popen([str(Path(plan['target_app'])/'Contents/MacOS/Lulu')],
                         close_fds=True, start_new_session=True, env=independent_environment())
    except Exception:
        # Do not overwrite a new transaction after releasing update.lock.
        receipt_error = RuntimeError('PRODUCT_OLD_TASK_SERVICE_UNAVAILABLE')
    if receipt_error is not None:
        from ..diagnostics import record_failure
        record_failure('update_handover_reopen', receipt_error,
                       root=updates_root.parent/'AgentEntry')
    return 5


def _run_helper(plan_path: str) -> int:
    path = Path(plan_path).resolve()
    updates_root = path.parents[2]
    transaction = InstallTransaction(updates_root)
    try:
        plan = transaction.load_plan(path)
    except Exception as exc:
        # Unverified input must never release an unrelated update lock.
        raise UpdateError('plan', '安装计划无法验证，已保留恢复记录') from exc
    def reject_wait(code, exit_code):
        fail_before_swap(path, plan, transaction, code, exit_code)
        # The GUI may have exited just before this failure. Reopen only when
        # it is certainly stopped and the original bundle has not moved.
        from .install import _same_identity
        try:
            if (process_identity(plan['old_pid']) is None
                    and _same_identity(Path(plan['target_app']), plan['target_lstat'])):
                subprocess.Popen([str(Path(plan['target_app'])/'Contents/MacOS/Lulu')],
                    close_fds=True, start_new_session=True, env=independent_environment())
        except Exception as exc:
            from ..diagnostics import record_failure
            record_failure('update_wait_reopen', exc, root=updates_root.parent/'AgentEntry')
        return exit_code
    try:
        identity = process_identity(int(plan['old_pid']))
        if identity is not None and not same_identity(identity, plan['old_pid_start_time'], plan['old_executable_path']):
            return reject_wait('old_process_changed', 3)
        if plan.get('launch_protocol') == 1:
            accept(path, plan, transaction)
        deadline = time.monotonic() + 30
        while identity is not None:
            if not same_identity(identity, plan['old_pid_start_time'], plan['old_executable_path']):
                return reject_wait('old_process_changed', 3)
            if time.monotonic() >= deadline:
                return reject_wait('old_process_timeout', 4)
            time.sleep(0.1)
            identity = process_identity(int(plan['old_pid']))
    except UpdateError as exc:
        return reject_wait(exc.code, 5)

    try:
        ensure_install_space(Path(plan['staged_app']), Path(plan['target_parent']))
    except UpdateError as exc:
        return reject_wait(exc.code, 5)
    handover = PreSwapHandover(path, plan)
    try:
        handover.prepare()
    except Exception as exc:
        # No exchange has been attempted. Never enter swap recovery or stop an
        # active old task just because export failed or a concurrent job won.
        return _abort_before_swap(transaction, updates_root, plan, handover, exc)

    launched = {"child": None, "identity": None}
    def launch(target, current_plan):
        # The old service is stopped and the App has been exchanged. Only now
        # may the new generation accept normal business requests.
        handover.release()
        executable = target / "Contents" / "MacOS" / "Lulu"
        child = subprocess.Popen([str(executable), "--lulu-update-ready", str(path), AUTHORIZATION_WAIT_FLAG], close_fds=True, start_new_session=True, env=independent_environment())
        launched["child"] = child
        try:
            identity_deadline = time.monotonic() + 5
            while time.monotonic() < identity_deadline:
                if child.poll() is not None:
                    raise RuntimeError("child exited before initialization")
                identity = process_identity(child.pid)
                if identity is not None:
                    started, actual_executable = identity
                    if Path(actual_executable).resolve() != executable.resolve():
                        raise RuntimeError("child executable mismatch")
                    launched["identity"] = (child.pid, started, actual_executable)
                    return launched["identity"]
                time.sleep(0.05)
            raise RuntimeError("child identity unavailable")
        except Exception:
            stop_update_child(child)
            raise

    def wait_ready(ready_path, current_plan, timeout):
        try:
            ready = wait_for_child_ready(
                transaction, ready_path, current_plan, launched["child"],
                launched["identity"], timeout=timeout,
            )
            if not ready:
                stop_update_core(current_plan)
            return ready
        except RollbackBlocked:
            raise
        except Exception:
            stop_update_child(launched["child"])
            stop_update_core(current_plan)
            raise

    cleanup_attempted = False
    def cleanup(current_plan):
        nonlocal cleanup_attempted
        cleanup_attempted = True
        try:
            cleanup_successful_update(updates_root, current_plan)
        except Exception as error:
            try:
                record_cleanup_failure(updates_root, current_plan, error)
            except Exception:
                pass

    recovery_blocked = False
    try:
        result = transaction.execute_local(
            path,
            launch=launch,
            wait_ready=wait_ready,
            old_pid_validator=lambda pid, _started, _exe: process_identity(pid) is None,
            on_installed=cleanup,
        )
    except RollbackBlocked:
        recovery_blocked = True
        result = "failed"
    except Exception as exc:
        if isinstance(exc, UpdateError) and exc.code == 'process_check' and launched['child'] is None:
            # execute_local queries the old PID before copying or exchanging.
            # Our preflight already stopped Core, so restore it on this exit.
            return _abort_before_swap(transaction, updates_root, plan, handover, exc)
        try:
            stop_update_child(launched["child"])
            stop_update_core(plan)
        except RollbackBlocked:
            recovery_blocked = True
            result = "failed"
        else:
            result = transaction.recover(path)
    finally:
        handover.release()
    if result == "installed" and not cleanup_attempted:
        try:
            transaction.acquire(plan["txn"])
            try:
                cleanup(plan)
            finally:
                transaction.release(expected_txn=plan["txn"])
        except Exception as error:
            # Installation success is authoritative. Cache cleanup is deliberately
            # best-effort and must never turn a working update into a rollback/error.
            try:
                record_cleanup_failure(updates_root, plan, error)
            except Exception:
                pass
    try:
        snapshot = StateStore(updates_root).load()
    except Exception as exc:
        # A read failure cannot prove recovery finished. Preserve the owner.
        from ..diagnostics import record_failure
        record_failure('update_helper_result_read', exc, root=updates_root.parent/'AgentEntry')
        _reopen_untouched_app(path)
        return 6
    mapping = {"installed": ("installed", "installed"), "rolled_back": ("rolled_back", "install_error_rollback"), "failed": ("failed", "rollback_failed")}
    # Successful transaction results were persisted before lock release. Do not
    # overwrite a new check/download started after that durable completion.
    if result not in {"installed", "rolled_back"} or snapshot.state in {"idle", "installing", "waiting_ready", "recovering"}:
        snapshot.state, snapshot.ui_state = mapping.get(result, ("failed", "rollback_failed"))
        if recovery_blocked:
            snapshot.error_code = "update_child_still_running"
        StateStore(updates_root).save(snapshot)
    if result == "rolled_back":
        old_executable = Path(plan["target_app"]) / "Contents" / "MacOS" / "Lulu"
        try:
            subprocess.Popen([str(old_executable)], close_fds=True, start_new_session=True, env=independent_environment())
        except Exception:
            pass
    return 0 if result == "installed" else 5


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if len(args)!=1: return 2
    return run_helper(args[0])


if __name__=="__main__": raise SystemExit(main())
