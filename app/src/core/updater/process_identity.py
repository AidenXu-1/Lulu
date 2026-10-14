from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from .models import UpdateError


def process_identity(pid: int) -> tuple[str, str] | None:
    """Identity of an executing process; absent and unreaped exits return None.

    Keep comm last: macOS truncates it when another output column follows it.
    A stopped (T) process can resume and is still live; only Z is terminated.
    """
    deadline = time.monotonic() + 0.3
    for attempt in range(7):
        try:
            # comm excludes arguments, so paths containing spaces stay intact.
            timeout = 3 if attempt == 0 else max(0.001, deadline - time.monotonic())
            result = subprocess.run(["/bin/ps", "-ww", "-p", str(int(pid)), "-o", "stat=", "-o", "lstart=", "-o", "comm="], capture_output=True, timeout=timeout, check=False, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError('process_check', '无法确认程序是否已退出，保留当前程序，请稍后重试') from exc
        line = result.stdout.strip()
        if result.returncode != 0 or not line:
            # A failed query is not evidence of exit. Only ESRCH confirms absence.
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return None
            except OSError as exc:
                raise UpdateError('process_check', '无法确认程序状态，已保留当前程序') from exc
            error = '程序仍存在但身份不可读，已保留当前程序'
        else:
            parts = line.split(None, 6)
            if len(parts) == 7 and parts[0][0] in 'RSDITZUXW':
                if parts[0].startswith('Z'):
                    return None
                if Path(parts[6]).is_absolute():
                    return " ".join(parts[1:6]), parts[6]
            error = '程序身份回复不完整，已保留当前程序'
        # macOS exit can briefly report ?E / ? or S with comm=(name).
        # Observe again; none of these alone proves exit or a changed identity.
        remaining = deadline - time.monotonic()
        if attempt == 6 or remaining <= 0:
            raise UpdateError('process_check', error)
        time.sleep(min(0.05, remaining))


def current_identity() -> tuple[str, str]:
    value = process_identity(os.getpid())
    if value is None:
        raise UpdateError('process_check', '无法确认当前程序身份，已停止更新')
    return value


def matches_process(pid: int, started: str, executable: str) -> bool:
    return same_identity(process_identity(pid), started, executable)


def same_identity(value: tuple[str, str] | None, started: str, executable: str) -> bool:
    """Compare one observation, without reopening an exit race with another ps."""
    if value is None:
        return False
    actual_started, actual_executable = value
    try:
        return actual_started == started and Path(actual_executable).resolve() == Path(executable).resolve()
    except Exception:
        return False
