"""Atomic, ownership-aware file/tree effects used by Agent entry transactions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Protocol

from .transaction import EffectProbe


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        chunks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def object_digest(path: Path) -> str:
    target = Path(path)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    if stat.S_ISREG(metadata.st_mode):
        return hashlib.sha256(
            b"file\0" + f"{stat.S_IMODE(metadata.st_mode):04o}".encode() + b"\0" + _read_regular(target)
        ).hexdigest()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    records = []
    for item in sorted(target.rglob("*"), key=lambda value: value.relative_to(target).as_posix()):
        child = item.lstat()
        if stat.S_ISLNK(child.st_mode):
            raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
        if stat.S_ISREG(child.st_mode):
            raw = _read_regular(item)
            records.append({"path": item.relative_to(target).as_posix(), "mode": f"{stat.S_IMODE(child.st_mode):04o}", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        elif not stat.S_ISDIR(child.st_mode):
            raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_exact(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    elif stat.S_ISREG(metadata.st_mode):
        path.unlink()
    else:
        raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")


def _assert_safe_ancestors(path):
    path=Path(path)
    for ancestor in path.parents:
        if ancestor.is_symlink() and ancestor not in {Path('/var'),Path('/tmp')}:
            raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')


class ManagedPathEffect:
    def __init__(
        self,
        *,
        name: str,
        source: Path,
        target: Path,
        work_root: Path,
        expected_before_digest: str | None = None,
    ) -> None:
        if name not in {"skill", "launcher", "core_registration"}:
            raise ValueError("invalid managed effect")
        self.name = name
        self.source = Path(source)
        self.target = Path(target)
        self.work_root = Path(work_root)
        self.expected_before_digest = expected_before_digest
        for path in (self.source,self.target,self.work_root/"stage"):_assert_safe_ancestors(path)
        self.after_digest = object_digest(self.source)
        self.stage = self.work_root / "stage"
        self.backup = self.work_root / "backup"
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.work_root, 0o700)

    def probe(self) -> EffectProbe:
        _assert_safe_ancestors(self.target)
        _assert_safe_ancestors(self.work_root/"stage")
        try:
            digest = object_digest(self.target)
        except FileNotFoundError:
            return EffectProbe("absent")
        except RuntimeError:
            return EffectProbe("third_party")
        if digest == self.after_digest:
            return EffectProbe("after")
        if self.expected_before_digest is not None and digest == self.expected_before_digest:
            return EffectProbe("before")
        if self.stage.exists() or self.backup.exists():
            return EffectProbe("drift")
        return EffectProbe("third_party")

    def _copy_stage(self) -> None:
        _assert_safe_ancestors(self.source)
        _assert_safe_ancestors(self.target)
        _assert_safe_ancestors(self.stage)
        if self.stage.exists():
            _remove_exact(self.stage)
        if self.source.is_dir():
            shutil.copytree(self.source, self.stage, symlinks=True)
        else:
            self.stage.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self.stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), stat.S_IMODE(self.source.stat().st_mode))
            try:
                raw = _read_regular(self.source)
                offset = 0
                while offset < len(raw):
                    offset += os.write(descriptor, raw[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if object_digest(self.stage) != self.after_digest:
            raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")

    def apply(self) -> None:
        state = self.probe().state
        if state == "after":
            return
        if state not in {"absent", "before"}:
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
        self.target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._copy_stage()
        if self.probe().state != state:
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
        _assert_safe_ancestors(self.target)
        if state == "before":
            if self.backup.exists():
                raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
            os.replace(self.target, self.backup)
        try:
            os.replace(self.stage, self.target)
            _fsync_directory(self.target.parent)
        except Exception:
            if not self.target.exists() and self.backup.exists():
                os.replace(self.backup, self.target)
            raise
        if self.probe().state != "after":
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")

    def rollback(self) -> None:
        state = self.probe().state
        if state == "after":
            _remove_exact(self.target)
        elif state not in {"absent", "before"}:
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
        if self.backup.exists():
            if self.target.exists():
                raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
            os.replace(self.backup, self.target)
        _fsync_directory(self.target.parent)
        expected = "before" if self.expected_before_digest is not None else "absent"
        if self.probe().state != expected:
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")


class ServiceController(Protocol):
    def is_loaded(self) -> bool: ...
    def start(self, plist: Path) -> None: ...
    def stop(self) -> None: ...


class CoreRegistrationEffect:
    name = "core_registration"

    def __init__(
        self,
        *,
        source: Path,
        target: Path,
        work_root: Path,
        service: ServiceController,
        expected_before_digest: str | None = None,
    ) -> None:
        self._file = ManagedPathEffect(
            name="core_registration",
            source=source,
            target=target,
            work_root=work_root,
            expected_before_digest=expected_before_digest,
        )
        self._service = service

    def probe(self) -> EffectProbe:
        file_state = self._file.probe().state
        loaded = self._service.is_loaded()
        if file_state == "after" and loaded:
            return EffectProbe("after")
        if file_state == "absent" and not loaded:
            return EffectProbe("absent")
        if file_state == "before" and not loaded:
            return EffectProbe("before")
        if file_state == "third_party":
            return EffectProbe("third_party")
        return EffectProbe("drift")

    def apply(self) -> None:
        state = self.probe().state
        if state not in {"absent", "before", "after"}:
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
        if state != "after":
            self._file.apply()
            self._service.start(self._file.target)
        if self.probe().state != "after":
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")

    def rollback(self) -> None:
        state = self.probe().state
        if state == "after":
            self._service.stop()
            self._file.rollback()
        elif state not in {"absent", "before"}:
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")


class CurrentUserLaunchctlService:
    """Fixed current-login-session service; there is intentionally no fallback."""

    def __init__(self, *, user_id: int | None = None, label: str = "com.lulu.transcribe.agent-core") -> None:
        self._uid = os.getuid() if user_id is None else user_id
        import re
        if self._uid != os.getuid() or (label != "com.lulu.transcribe.agent-core" and
                re.fullmatch(r"com\.lulu\.transcribe\.agent-core\.app-[a-f0-9]{64}", label) is None):
            raise ValueError("CALLER_USER_MISMATCH")
        self._domain = f"gui/{self._uid}"
        self._service = f"{self._domain}/{label}"

    @staticmethod
    def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )

    def is_loaded(self) -> bool:
        result = self._run(["print", self._service])
        return result.returncode == 0

    def start(self, plist: Path) -> None:
        if self.is_loaded():
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
        result = self._run(["bootstrap", self._domain, str(plist)])
        if result.returncode != 0 or not self.is_loaded():
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")

    def stop(self) -> None:
        if not self.is_loaded():
            return
        result = self._run(["bootout", self._service])
        if result.returncode != 0:
            raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
        # bootout acknowledges removal before a running service fully exits.
        # Do not replace product files until launchd confirms it is gone.
        deadline = time.monotonic() + 30
        while self.is_loaded():
            if time.monotonic() >= deadline:
                raise RuntimeError("AGENT_ENTRY_TRANSACTION_DRIFT")
            time.sleep(0.1)

    def terminate_registered_process(self) -> None:
        """Release old Core memory while retaining launchd's rollback entry."""
        import re
        def running():
            result = self._run(['print', self._service])
            return result.returncode == 0 and re.search(r'^\s*pid = [1-9][0-9]*\s*$', result.stdout, re.M) is not None
        if not running():
            return
        self._run(['kill', 'SIGTERM', self._service])
        deadline = time.monotonic()+4
        while running():
            if time.monotonic() >= deadline:
                raise RuntimeError('PRODUCT_CORE_STILL_RUNNING')
            time.sleep(.1)
