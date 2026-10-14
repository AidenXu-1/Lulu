"""Trusted Test only isolated E2E runtime and stdlib bootstrap primitives."""

from __future__ import annotations

import json
import base64
import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import hmac
import importlib.abc
import importlib.util
import os
from pathlib import Path
import pwd
import secrets
import shutil
import stat
import sys
import tempfile
import time
from typing import Callable, Mapping


SAFE_CREDENTIAL_ACCOUNTS = (
    "lulu.trusted-test.safe.license-cache.v1",
    "lulu.trusted-test.safe.ticket-hmac.v1",
    "lulu.trusted-test.safe.ticket-anchor.v1",
    "lulu.trusted-test.safe.ticket.v1",
)
_CREDENTIAL_FIELDS = ("license", "hmac", "anchor", "ticket")
_RUN_CHILDREN = (
    "authority",
    "credentials",
    "home",
    "cache",
    "tmp",
    "input",
    "logs",
    "receipts",
)
_CACHE_CHILDREN = (
    "xdg/config",
    "xdg/data",
    "xdg/cache",
    "huggingface",
    "torch",
    "modelscope",
    "matplotlib",
    "numba",
    "funasr",
    "pycache",
)


class SafeE2EError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


_AUTHORITY_SCHEMA = "lulu.trusted-test.safe-e2e-authority/v1"
_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "task_id",
        "slice_id",
        "run_root",
        "root_dev",
        "root_ino",
        "candidate_app",
        "executable_path",
        "executable_sha256",
        "ffmpeg_path",
        "ffmpeg_sha256",
        "sandbox_profile_path",
        "sandbox_profile_sha256",
        "build_profile_sha256",
        "source_commit",
        "source_tree",
        "model_root",
        "model_tree_sha256",
        "input_path",
        "input_sha256",
        "installation_id",
        "created_at",
        "expires_at",
    }
)
_SAFE_AUTHORITY_SEAL = object()
_RUNTIME_GUARDS_INSTALLED = False
_SAFE_MODEL_RELATIVE = Path(
    "home/Library/Application Support/Lulu/models/local_sensevoice"
)
_SAFE_SENSEVOICE_REQUIRED_FILES = (
    Path("SenseVoiceSmall/model.pt"),
    Path("fsmn-vad/model.pt"),
)
_SAFE_SENSEVOICE_RUNTIME_MODULES = (
    "funasr",
    "imageio_ffmpeg",
    "soundfile",
    "torch",
    "torchaudio",
)
_SAFE_JOB_ERROR_STAGES = {
    "AUTH_GUARD_REQUIRED": "authorization",
    "FRESH_CACHED_AUTH_REJECTED": "authorization",
    "SAFE_TEST_AUTHORITY_REQUIRED": "authorization",
    "SOURCE_INVALID": "source_validation",
    "SOURCE_NOT_FOUND": "source_validation",
    "SOURCE_STAT_FAILED": "source_validation",
    "SOURCE_NOT_REGULAR": "source_validation",
    "SOURCE_IDENTITY_CHANGED": "source_validation",
    "SOURCE_OPEN_FAILED": "source_validation",
    "MODEL_NOT_READY": "model_readiness",
    "MODEL_READINESS_PROBE_FAILED": "model_readiness",
    "LOCAL_TRANSCRIPTION_RUNTIME_INCOMPLETE": "runtime_incomplete",
}
_SAFE_SERVICE_ERROR_STAGES = {
    "AUTH_REQUIRED": "authorization",
    "SECURE_STORE_UNAVAILABLE": "authorization",
    "INVALID_ARGUMENT": "source_validation",
    "NOT_FOUND": "source_validation",
    "UNSAFE_PATH": "source_validation",
    "MODEL_NOT_READY": "model_readiness",
}


def _private_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SafeE2EError("PRIVATE_DIRECTORY_UNAVAILABLE") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SafeE2EError("PRIVATE_DIRECTORY_INVALID")
    return metadata


def _make_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise SafeE2EError("PRIVATE_DIRECTORY_CREATE_FAILED") from exc
    _private_directory(path)


def create_private_run_root(base: Path) -> Path:
    """Create one owner-only run root without accepting a caller-selected leaf."""

    if not isinstance(base, Path) or not base.is_absolute():
        raise SafeE2EError("RUN_BASE_INVALID")
    base_identity = _private_directory(base)
    run_id = secrets.token_hex(16)
    root = base / run_id
    _make_private_directory(root)
    root_identity = _private_directory(root)
    for relative in (*_RUN_CHILDREN, *(f"cache/{item}" for item in _CACHE_CHILDREN)):
        path = root / relative
        if not path.parent.exists():
            _make_private_directory(path.parent)
        _make_private_directory(path)
    namespace_identity = root.lstat()
    if (
        (base.lstat().st_dev, base.lstat().st_ino)
        != (base_identity.st_dev, base_identity.st_ino)
        or (namespace_identity.st_dev, namespace_identity.st_ino)
        != (root_identity.st_dev, root_identity.st_ino)
    ):
        raise SafeE2EError("RUN_ROOT_IDENTITY_CHANGED")
    return root


def assert_write_target(root: Path, target: Path) -> Path:
    """Reject a write path lexically and by existing ancestor before any create."""

    if not isinstance(root, Path) or not isinstance(target, Path):
        raise SafeE2EError("WRITE_PATH_INVALID")
    if not root.is_absolute() or not target.is_absolute() or ".." in target.parts:
        raise SafeE2EError("WRITE_OUTSIDE_ROOT")
    _private_directory(root)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SafeE2EError("WRITE_OUTSIDE_ROOT") from exc
    if not relative.parts:
        raise SafeE2EError("WRITE_OUTSIDE_ROOT")
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise SafeE2EError("WRITE_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SafeE2EError("WRITE_PATH_UNSAFE")
    return target


def isolated_environment(root: Path) -> dict[str, str]:
    _private_directory(root)
    mapping = {
        "HOME": root / "home",
        "TMPDIR": root / "tmp",
        "XDG_CONFIG_HOME": root / "cache/xdg/config",
        "XDG_DATA_HOME": root / "cache/xdg/data",
        "XDG_CACHE_HOME": root / "cache/xdg/cache",
        "HF_HOME": root / "cache/huggingface",
        "TORCH_HOME": root / "cache/torch",
        "MODELSCOPE_CACHE": root / "cache/modelscope",
        "MPLCONFIGDIR": root / "cache/matplotlib",
        "NUMBA_CACHE_DIR": root / "cache/numba",
        "FUNASR_HOME": root / "cache/funasr",
        "PYTHONPYCACHEPREFIX": root / "cache/pycache",
    }
    for path in mapping.values():
        _private_directory(path)
    return {key: str(path) for key, path in mapping.items()}


class _ForbiddenCredentialModuleFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        del path, target
        if fullname == "keyring" or fullname.startswith("keyring."):
            raise ImportError("KEYCHAIN_MODULE_FORBIDDEN")
        return None


def _directory_path_from_fd(dir_fd: object) -> Path | None:
    if dir_fd == -1:
        return None
    if type(dir_fd) is not int or dir_fd < 0:
        raise SafeE2EError("WRITE_PATH_INVALID")
    try:
        descriptor_metadata = os.fstat(dir_fd)
        if not stat.S_ISDIR(descriptor_metadata.st_mode):
            raise SafeE2EError("WRITE_PATH_INVALID")
        if sys.platform == "darwin":
            raw_path = fcntl.fcntl(dir_fd, 50, b"\0" * 1024)
            directory = Path(raw_path.split(b"\0", 1)[0].decode("utf-8"))
        else:
            directory = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
        path_metadata = directory.lstat()
    except SafeE2EError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise SafeE2EError("WRITE_PATH_INVALID") from exc
    if (
        not directory.is_absolute()
        or stat.S_ISLNK(path_metadata.st_mode)
        or not stat.S_ISDIR(path_metadata.st_mode)
        or (path_metadata.st_dev, path_metadata.st_ino)
        != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
    ):
        raise SafeE2EError("WRITE_PATH_INVALID")
    return directory


def _audit_path(value: object, *, dir_fd: object = -1) -> Path:
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value:
        raise SafeE2EError("WRITE_PATH_INVALID")
    path = Path(value)
    if not path.is_absolute():
        directory = _directory_path_from_fd(dir_fd)
        path = (directory or Path.cwd()) / path
    return path


def install_runtime_guards(
    root: Path,
    *,
    allowed_subprocesses: Mapping[Path, str] | None = None,
) -> None:
    """Install process-lifetime guards before importing product runtime code."""

    global _RUNTIME_GUARDS_INSTALLED
    _private_directory(root)
    if _RUNTIME_GUARDS_INSTALLED:
        raise SafeE2EError("RUNTIME_GUARDS_ALREADY_INSTALLED")
    root = Path(root)
    approved: dict[Path, str] = {}
    for path, digest in dict(allowed_subprocesses or {}).items():
        candidate = Path(path)
        try:
            candidate_metadata = candidate.lstat()
        except OSError as exc:
            raise SafeE2EError("SUBPROCESS_AUTHORITY_INVALID") from exc
        if (
            not candidate.is_absolute()
            or stat.S_ISLNK(candidate_metadata.st_mode)
            or not stat.S_ISREG(candidate_metadata.st_mode)
            or not os.access(candidate, os.X_OK)
            or type(digest) is not str
            or len(digest) != 64
            or _sha256_file(candidate) != digest
        ):
            raise SafeE2EError("SUBPROCESS_AUTHORITY_INVALID")
        approved[candidate] = digest

    def require_write_target(
        value: object,
        *,
        dir_fd: object = -1,
        allow_dev_null: bool = False,
    ) -> None:
        if isinstance(value, int):
            return
        target = _audit_path(value, dir_fd=dir_fd)
        if allow_dev_null and target == Path(os.devnull):
            return
        assert_write_target(root, target)

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open" and args:
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            writing = (
                isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "x", "+"))
            ) or (
                isinstance(flags, int)
                and bool(
                    flags
                    & (
                        os.O_WRONLY
                        | os.O_RDWR
                        | os.O_CREAT
                        | os.O_TRUNC
                        | os.O_APPEND
                    )
                )
            )
            if writing:
                require_write_target(args[0], allow_dev_null=True)
            return
        if event == "os.mkdir" and args:
            require_write_target(args[0], dir_fd=args[2] if len(args) > 2 else -1)
            return
        if event in {"os.remove", "os.rmdir", "os.unlink"} and args:
            require_write_target(args[0], dir_fd=args[1] if len(args) > 1 else -1)
            return
        if event == "os.chmod" and args:
            require_write_target(args[0], dir_fd=args[2] if len(args) > 2 else -1)
            return
        if event == "os.utime" and args:
            require_write_target(args[0], dir_fd=args[3] if len(args) > 3 else -1)
            return
        if event == "os.truncate" and args:
            require_write_target(args[0])
            return
        if event in {"os.rename", "os.replace"} and len(args) >= 2:
            require_write_target(args[0], dir_fd=args[2] if len(args) > 2 else -1)
            require_write_target(args[1], dir_fd=args[3] if len(args) > 3 else -1)
            return
        if event.startswith("socket."):
            raise SafeE2EError("NETWORK_FORBIDDEN")
        if event == "ctypes.dlopen" and args:
            library = os.fsdecode(args[0]) if isinstance(args[0], bytes) else str(args[0])
            if "Security.framework" in library or library.endswith("/Security"):
                raise SafeE2EError("KEYCHAIN_API_FORBIDDEN")
        if event == "subprocess.Popen" and args:
            executable = os.fsdecode(args[0]) if isinstance(args[0], bytes) else str(args[0])
            if executable == "ffmpeg":
                raise FileNotFoundError(
                    errno.ENOENT,
                    "SAFE_RELATIVE_FFMPEG_UNAVAILABLE",
                    executable,
                )
            executable_path = Path(executable)
            expected_digest = approved.get(executable_path)
            if expected_digest is None or _sha256_file(executable_path) != expected_digest:
                raise SafeE2EError("AUTHORIZATION_TOOL_FORBIDDEN")

    sys.meta_path.insert(0, _ForbiddenCredentialModuleFinder())
    sys.addaudithook(audit)
    _RUNTIME_GUARDS_INSTALLED = True


def _canonical_store_bytes(values: Mapping[str, str]) -> bytes:
    return (
        json.dumps(
            dict(values),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


class FileCredentialStore:
    """Exact-account credential store confined to one already-private directory."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise SafeE2EError("CREDENTIAL_PATH_INVALID")
        _private_directory(path.parent)
        assert_write_target(path.parent, path)
        self.path = path
        self.write_order: list[str] = []

    @staticmethod
    def _account(account: str) -> str:
        if type(account) is not str or account not in SAFE_CREDENTIAL_ACCOUNTS:
            raise SafeE2EError("CREDENTIAL_ACCOUNT_FORBIDDEN")
        return account

    def _read_all(self) -> dict[str, str]:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SafeE2EError("CREDENTIAL_STORE_UNAVAILABLE") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise SafeE2EError("CREDENTIAL_STORE_INVALID")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise SafeE2EError("CREDENTIAL_STORE_INVALID") from exc
        if (
            type(payload) is not dict
            or any(
                key not in SAFE_CREDENTIAL_ACCOUNTS or type(value) is not str
                for key, value in payload.items()
            )
        ):
            raise SafeE2EError("CREDENTIAL_STORE_INVALID")
        return payload

    def read(self, account: str) -> str | None:
        return self._read_all().get(self._account(account))

    def write(self, account: str, value: str) -> None:
        account = self._account(account)
        if type(value) is not str or not value:
            raise SafeE2EError("CREDENTIAL_VALUE_INVALID")
        values = self._read_all()
        if account in values:
            raise SafeE2EError("CREDENTIAL_ALREADY_EXISTS")
        values[account] = value
        raw = _canonical_store_bytes(values)
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(16)}.tmp"
        assert_write_target(self.path.parent, temporary)
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("short credential write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            directory_descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise SafeE2EError("CREDENTIAL_WRITE_FAILED") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if self._read_all() != values:
            raise SafeE2EError("CREDENTIAL_READBACK_FAILED")
        self.write_order.append(account)

    def replace(self, account: str, expected: str, replacement: str) -> None:
        account = self._account(account)
        if account != SAFE_CREDENTIAL_ACCOUNTS[2]:
            raise SafeE2EError("CREDENTIAL_REPLACE_FORBIDDEN")
        if type(expected) is not str or type(replacement) is not str or not replacement:
            raise SafeE2EError("CREDENTIAL_VALUE_INVALID")
        values = self._read_all()
        if values.get(account) != expected:
            raise SafeE2EError("CREDENTIAL_COMPARE_AND_SET_FAILED")
        values[account] = replacement
        self._commit(values)

    def _commit(self, values: Mapping[str, str]) -> None:
        raw = _canonical_store_bytes(values)
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(16)}.tmp"
        assert_write_target(self.path.parent, temporary)
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("short credential write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            directory_descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise SafeE2EError("CREDENTIAL_WRITE_FAILED") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if self._read_all() != dict(values):
            raise SafeE2EError("CREDENTIAL_READBACK_FAILED")


def provision_test_credentials(
    store: FileCredentialStore,
    secret_values: Mapping[str, str],
) -> dict[str, object]:
    if not isinstance(store, FileCredentialStore):
        raise SafeE2EError("CREDENTIAL_STORE_REQUIRED")
    if type(secret_values) is not dict or tuple(secret_values) != _CREDENTIAL_FIELDS:
        raise SafeE2EError("CREDENTIAL_INPUT_INVALID")
    for account, field in zip(SAFE_CREDENTIAL_ACCOUNTS, _CREDENTIAL_FIELDS):
        value = secret_values.get(field)
        if type(value) is not str or not value:
            raise SafeE2EError("CREDENTIAL_INPUT_INVALID")
        store.write(account, value)
    return {
        "schema": "lulu.trusted-test.safe-credentials-receipt/v1",
        "accounts_present": list(SAFE_CREDENTIAL_ACCOUNTS),
        "ticket_last": store.write_order[-1] == SAFE_CREDENTIAL_ACCOUNTS[-1],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SafeE2EError("FILE_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SafeE2EError("FILE_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SafeE2EError("FILE_IDENTITY_CHANGED")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise SafeE2EError("MODEL_ROOT_UNAVAILABLE") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise SafeE2EError("MODEL_ROOT_UNSAFE")
    records: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SafeE2EError("MODEL_TREE_UNAVAILABLE") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise SafeE2EError("MODEL_TREE_SYMLINK_FORBIDDEN")
            if stat.S_ISDIR(metadata.st_mode):
                records.append({"path": relative, "type": "dir"})
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "bytes": metadata.st_size,
                        "sha256": _sha256_file(path),
                    }
                )
            else:
                raise SafeE2EError("MODEL_TREE_SPECIAL_FILE_FORBIDDEN")

    visit(root)
    raw = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _authority_bound_model_readiness(
    receipt: SafeAuthorityReceipt,
) -> Callable[[str], bool]:
    if not isinstance(receipt, SafeAuthorityReceipt) or receipt._seal is not _SAFE_AUTHORITY_SEAL:
        raise SafeE2EError("AUTHORITY_RECEIPT_REQUIRED")
    run_root = Path(str(receipt["run_root"]))
    model_root = Path(str(receipt["model_root"]))
    expected_model_root = run_root / _SAFE_MODEL_RELATIVE
    expected_digest = str(receipt["model_tree_sha256"])

    def ready(engine_name: str) -> bool:
        if engine_name != "local_sensevoice" or model_root != expected_model_root:
            return False
        try:
            model_root.relative_to(run_root)
            root_metadata = model_root.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != os.getuid()
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                return False
            pending = [model_root]
            while pending:
                current = pending.pop()
                with os.scandir(current) as entries:
                    children = list(entries)
                for entry in children:
                    metadata = entry.stat(follow_symlinks=False)
                    if metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode):
                        return False
                    if stat.S_ISDIR(metadata.st_mode):
                        if stat.S_IMODE(metadata.st_mode) != 0o700:
                            return False
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(metadata.st_mode):
                        if stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_nlink != 1:
                            return False
                    else:
                        return False
            for relative in _SAFE_SENSEVOICE_REQUIRED_FILES:
                target = model_root / relative
                metadata = target.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                    or metadata.st_nlink != 1
                ):
                    return False
            if _tree_digest(model_root) != expected_digest:
                return False
        except (OSError, SafeE2EError, ValueError):
            return False
        if any(
            importlib.util.find_spec(name) is None
            for name in _SAFE_SENSEVOICE_RUNTIME_MODULES
        ):
            from .core.transcription_job_application import LocalTranscriptionJobError

            raise LocalTranscriptionJobError(
                "RUNTIME_NOT_READY",
                "LOCAL_TRANSCRIPTION_RUNTIME_INCOMPLETE",
            )
        return True

    return ready


@contextmanager
def _discard_third_party_stdout() -> Iterator[None]:
    """Keep Python and native fd writes out of the public JSON stdout channel."""

    original_stdout = sys.stdout
    saved_stdout_fd = os.dup(1)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            try:
                original_stdout.flush()
            except (AttributeError, OSError, ValueError):
                pass
            os.dup2(sink.fileno(), 1)
            sys.stdout = sink
            try:
                yield
            finally:
                try:
                    sink.flush()
                finally:
                    sys.stdout = original_stdout
                    os.dup2(saved_stdout_fd, 1)
    finally:
        os.close(saved_stdout_fd)


def _authority_bound_pipeline_runner(
    run_root: Path,
    readiness: Callable[[str], bool],
):
    _private_directory(run_root)
    temporary_root = run_root / "tmp"
    _private_directory(temporary_root)

    def runner(source_path, *, engine_name, progress, is_cancelled):
        if not readiness(engine_name):
            from .core.transcription_job_application import LocalTranscriptionJobError

            raise LocalTranscriptionJobError("MODEL_NOT_READY", "MODEL_NOT_READY")
        with _discard_third_party_stdout():
            from .core.audio.converter import to_wav_16k_mono
            from .core.engines.local_sensevoice import LocalSenseVoiceEngine

            with tempfile.TemporaryDirectory(
                prefix="safe-e2e-audio-",
                dir=temporary_root,
            ) as temporary:
                wav_path = to_wav_16k_mono(
                    Path(source_path),
                    Path(temporary) / "audio_16k.wav",
                )
                engine = LocalSenseVoiceEngine()
                return engine.transcribe(
                    wav_path,
                    progress,
                    is_cancelled=is_cancelled,
                )

    return runner


def _mkdir_private_chain(root: Path, relative: Path) -> Path:
    current = root
    _private_directory(current)
    for component in relative.parts:
        current = current / component
        if not current.exists():
            _make_private_directory(current)
        else:
            _private_directory(current)
    return current


def _copy_regular_file(source: Path, target: Path, *, mode: int) -> None:
    assert_write_target(target.parent, target)
    if target.exists() or target.is_symlink():
        raise SafeE2EError("SOURCE_TARGET_EXISTS")
    temporary = target.parent / f".{target.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        source_metadata = source.lstat()
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
            raise SafeE2EError("SOURCE_FILE_UNSAFE")
        source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
            while True:
                block = os.read(source_descriptor, 1024 * 1024)
                if not block:
                    break
                offset = 0
                while offset < len(block):
                    written = os.write(descriptor, block[offset:])
                    if written <= 0:
                        raise OSError("short copy write")
                    offset += written
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
        finally:
            os.close(source_descriptor)
        os.replace(temporary, target)
        os.chmod(target, mode, follow_symlinks=False)
    except SafeE2EError:
        raise
    except OSError as exc:
        raise SafeE2EError("SOURCE_COPY_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_model_tree(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise SafeE2EError("MODEL_TARGET_EXISTS")
    _make_private_directory(target)

    def visit(source_dir: Path, target_dir: Path) -> None:
        for entry in sorted(os.scandir(source_dir), key=lambda item: item.name):
            source_path = Path(entry.path)
            target_path = target_dir / entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise SafeE2EError("MODEL_TREE_SYMLINK_FORBIDDEN")
            if stat.S_ISDIR(metadata.st_mode):
                _make_private_directory(target_path)
                visit(source_path, target_path)
            elif stat.S_ISREG(metadata.st_mode):
                _copy_regular_file(source_path, target_path, mode=0o400)
            else:
                raise SafeE2EError("MODEL_TREE_SPECIAL_FILE_FORBIDDEN")

    visit(source, target)


def _write_private_json(path: Path, payload: Mapping[str, object]) -> bytes:
    raw = (
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    assert_write_target(path.parent, temporary)
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short authority write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if path.exists() or path.is_symlink():
            raise SafeE2EError("AUTHORITY_ALREADY_EXISTS")
        os.replace(temporary, path)
    except SafeE2EError:
        raise
    except OSError as exc:
        raise SafeE2EError("AUTHORITY_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if path.read_bytes() != raw:
        raise SafeE2EError("AUTHORITY_READBACK_FAILED")
    return raw


def _write_private_bytes(path: Path, raw: bytes) -> None:
    if type(raw) is not bytes or not raw:
        raise SafeE2EError("PRIVATE_FILE_CONTENT_INVALID")
    assert_write_target(path.parent, path)
    if path.exists() or path.is_symlink():
        raise SafeE2EError("PRIVATE_FILE_ALREADY_EXISTS")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short private write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise SafeE2EError("PRIVATE_FILE_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if path.read_bytes() != raw:
        raise SafeE2EError("PRIVATE_FILE_READBACK_FAILED")


def _sandbox_profile_bytes(
    run_root: Path,
    executable_path: Path,
    ffmpeg_path: Path,
) -> bytes:
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    formal_root = account_home / "Library/Application Support/Lulu"
    keychain_root = account_home / "Library/Keychains"
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write* (require-all "
        f"(require-not (subpath {quote(run_root)})) "
        f"(require-not (literal {quote(Path(os.devnull))}))))\n"
        f"(deny file-read* (subpath {quote(formal_root)}))\n"
        f"(deny file-read* (subpath {quote(keychain_root)}))\n"
        "(deny process-exec (require-not (require-any "
        f"(literal {quote(executable_path)}) "
        f"(literal {quote(ffmpeg_path)}))))\n"
        '(deny mach-lookup (global-name "com.apple.securityd"))\n'
    ).encode("utf-8")


def _read_profile(candidate_app: Path) -> tuple[dict[str, object], Path]:
    profile_path = candidate_app / "Contents/Resources/build_profile.json"
    try:
        metadata = profile_path.lstat()
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SafeE2EError("CANDIDATE_PROFILE_INVALID") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or type(payload) is not dict:
        raise SafeE2EError("CANDIDATE_PROFILE_INVALID")
    if (
        payload.get("profile") != "trusted_test_safe_e2e"
        or payload.get("package_surface") != "isolated_local_transcription_e2e/v1"
        or not isinstance(payload.get("source_commit"), str)
        or len(str(payload.get("source_commit"))) != 40
        or not isinstance(payload.get("source_tree"), str)
        or len(str(payload.get("source_tree"))) != 40
    ):
        raise SafeE2EError("CANDIDATE_PROFILE_MISMATCH")
    return payload, profile_path


def prepare_isolated_authority(
    *,
    run_root: Path,
    candidate_app: Path,
    model_seed: Path,
    source_file: Path,
    now_epoch: int | None = None,
) -> dict[str, object]:
    """Prepare one fully rooted authority. This runs outside the candidate."""

    root_metadata = _private_directory(run_root)
    for child in _RUN_CHILDREN:
        _private_directory(run_root / child)
    if not candidate_app.is_absolute() or candidate_app.is_symlink() or not candidate_app.is_dir():
        raise SafeE2EError("CANDIDATE_APP_INVALID")
    profile, profile_path = _read_profile(candidate_app)
    executable = candidate_app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
    try:
        executable_metadata = executable.lstat()
    except OSError as exc:
        raise SafeE2EError("CANDIDATE_EXECUTABLE_INVALID") from exc
    if (
        not executable.is_absolute()
        or stat.S_ISLNK(executable_metadata.st_mode)
        or not stat.S_ISREG(executable_metadata.st_mode)
        or not os.access(executable, os.X_OK)
    ):
        raise SafeE2EError("CANDIDATE_EXECUTABLE_INVALID")
    executable_sha256 = _sha256_file(executable)
    ffmpeg_directory = candidate_app / "Contents/Frameworks/imageio_ffmpeg/binaries"
    try:
        ffmpeg_candidates = [
            Path(entry.path)
            for entry in os.scandir(ffmpeg_directory)
            if entry.name.startswith("ffmpeg-")
            and stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)
            and not stat.S_ISLNK(entry.stat(follow_symlinks=False).st_mode)
        ]
    except OSError as exc:
        raise SafeE2EError("CANDIDATE_FFMPEG_INVALID") from exc
    if len(ffmpeg_candidates) != 1:
        raise SafeE2EError("CANDIDATE_FFMPEG_INVALID")
    ffmpeg_path = ffmpeg_candidates[0]
    if not os.access(ffmpeg_path, os.X_OK):
        raise SafeE2EError("CANDIDATE_FFMPEG_INVALID")
    ffmpeg_sha256 = _sha256_file(ffmpeg_path)
    seed_digest_before = _tree_digest(model_seed)

    application_support = _mkdir_private_chain(run_root / "home", Path("Library/Application Support"))
    product_root = _mkdir_private_chain(application_support, Path("Lulu"))
    models_root = _mkdir_private_chain(product_root, Path("models"))
    model_root = models_root / "local_sensevoice"
    _copy_model_tree(model_seed, model_root)
    model_digest = _tree_digest(model_root)
    if model_digest != seed_digest_before or _tree_digest(model_seed) != seed_digest_before:
        raise SafeE2EError("MODEL_COPY_MISMATCH")

    suffix = source_file.suffix.lower()
    if not suffix or len(suffix) > 12:
        suffix = ".audio"
    input_path = run_root / "input" / f"source{suffix}"
    _copy_regular_file(source_file, input_path, mode=0o400)
    if _sha256_file(source_file) != _sha256_file(input_path):
        raise SafeE2EError("INPUT_COPY_MISMATCH")

    now_value = int(time.time() if now_epoch is None else now_epoch)
    installation_id = f"safe-{secrets.token_hex(16)}"
    sandbox_profile_path = run_root / "authority/runtime.sb"
    sandbox_raw = _sandbox_profile_bytes(run_root, executable, ffmpeg_path)
    _write_private_bytes(sandbox_profile_path, sandbox_raw)
    payload: dict[str, object] = {
        "schema": _AUTHORITY_SCHEMA,
        "task_id": "TASK-20260902-0C563E",
        "slice_id": "SLICE-20260902-562CE6",
        "run_root": str(run_root),
        "root_dev": root_metadata.st_dev,
        "root_ino": root_metadata.st_ino,
        "candidate_app": str(candidate_app),
        "executable_path": str(executable),
        "executable_sha256": executable_sha256,
        "ffmpeg_path": str(ffmpeg_path),
        "ffmpeg_sha256": ffmpeg_sha256,
        "sandbox_profile_path": str(sandbox_profile_path),
        "sandbox_profile_sha256": hashlib.sha256(sandbox_raw).hexdigest(),
        "build_profile_sha256": _sha256_file(profile_path),
        "source_commit": profile["source_commit"],
        "source_tree": profile["source_tree"],
        "model_root": str(model_root),
        "model_tree_sha256": model_digest,
        "input_path": str(input_path),
        "input_sha256": _sha256_file(input_path),
        "installation_id": installation_id,
        "created_at": now_value,
        "expires_at": now_value + 3 * 60 * 60,
    }
    authority_path = run_root / "authority/authority.json"
    raw_authority = _write_private_json(authority_path, payload)
    hmac_key = secrets.token_bytes(32)
    ticket_payload = {
        "schema": "lulu.trusted-test.safe-ticket/v1",
        "installation_id": installation_id,
        "authority_sha256": hashlib.sha256(raw_authority).hexdigest(),
        "signature": hmac.new(hmac_key, raw_authority, hashlib.sha256).hexdigest(),
    }
    credential_values = {
        "license": json.dumps(
            {"allowed": True, "expires_at": payload["expires_at"]},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "hmac": base64.b64encode(hmac_key).decode("ascii"),
        "anchor": "null",
        "ticket": json.dumps(ticket_payload, sort_keys=True, separators=(",", ":")),
    }
    credential_receipt = provision_test_credentials(
        FileCredentialStore(run_root / "credentials/store.json"),
        credential_values,
    )
    return {
        "schema": "lulu.trusted-test.safe-prepare-receipt/v1",
        "authority_path": str(authority_path),
        "run_root": str(run_root),
        "candidate_sha256": executable_sha256,
        "model_tree_sha256": model_digest,
        "input_sha256": payload["input_sha256"],
        "credentials": credential_receipt,
    }


class SafeAuthorityReceipt(Mapping[str, object]):
    __slots__ = ("_payload", "_raw", "_seal")

    def __init__(self, payload: dict[str, object], raw: bytes, seal: object) -> None:
        if seal is not _SAFE_AUTHORITY_SEAL:
            raise SafeE2EError("AUTHORITY_RECEIPT_FORBIDDEN")
        self._payload = dict(payload)
        self._raw = bytes(raw)
        self._seal = seal

    def __getitem__(self, key: str) -> object:
        return self._payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._payload)

    def __len__(self) -> int:
        return len(self._payload)


def _validate_test_ticket(receipt: SafeAuthorityReceipt) -> FileCredentialStore:
    root = Path(str(receipt["run_root"]))
    store = FileCredentialStore(root / "credentials/store.json")
    raw_hmac = store.read(SAFE_CREDENTIAL_ACCOUNTS[1])
    raw_ticket = store.read(SAFE_CREDENTIAL_ACCOUNTS[3])
    if raw_hmac is None or raw_ticket is None:
        raise SafeE2EError("SAFE_TEST_TICKET_REQUIRED")
    try:
        key = base64.b64decode(raw_hmac, validate=True)
        ticket = json.loads(raw_ticket)
    except Exception as exc:  # noqa: BLE001
        raise SafeE2EError("SAFE_TEST_TICKET_INVALID") from exc
    expected_signature = hmac.new(key, receipt._raw, hashlib.sha256).hexdigest()
    if (
        len(key) < 32
        or type(ticket) is not dict
        or ticket.get("schema") != "lulu.trusted-test.safe-ticket/v1"
        or ticket.get("installation_id") != receipt["installation_id"]
        or ticket.get("authority_sha256") != hashlib.sha256(receipt._raw).hexdigest()
        or not hmac.compare_digest(str(ticket.get("signature") or ""), expected_signature)
    ):
        raise SafeE2EError("SAFE_TEST_TICKET_INVALID")
    return store


def load_safe_authority(
    authority_path: Path,
    *,
    executable_path: Path,
    now_epoch: int | None = None,
) -> SafeAuthorityReceipt:
    if not isinstance(authority_path, Path) or not authority_path.is_absolute():
        raise SafeE2EError("AUTHORITY_PATH_INVALID")
    try:
        metadata = authority_path.lstat()
        raw = authority_path.read_bytes()
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise SafeE2EError("AUTHORITY_INVALID") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or type(payload) is not dict
        or frozenset(payload) != _AUTHORITY_FIELDS
        or payload.get("schema") != _AUTHORITY_SCHEMA
    ):
        raise SafeE2EError("AUTHORITY_INVALID")
    root = Path(str(payload["run_root"]))
    root_metadata = _private_directory(root)
    if authority_path != root / "authority/authority.json":
        raise SafeE2EError("AUTHORITY_PATH_MISMATCH")
    if (root_metadata.st_dev, root_metadata.st_ino) != (payload["root_dev"], payload["root_ino"]):
        raise SafeE2EError("AUTHORITY_ROOT_MISMATCH")
    now_value = int(time.time() if now_epoch is None else now_epoch)
    if (
        type(payload["created_at"]) is not int
        or type(payload["expires_at"]) is not int
        or not payload["created_at"] <= now_value <= payload["expires_at"]
    ):
        raise SafeE2EError("AUTHORITY_EXPIRED")
    try:
        executable_metadata = executable_path.lstat()
        executable_sha256 = _sha256_file(executable_path)
    except (OSError, SafeE2EError) as exc:
        raise SafeE2EError("AUTHORITY_EXECUTABLE_MISMATCH") from exc
    if (
        not executable_path.is_absolute()
        or stat.S_ISLNK(executable_metadata.st_mode)
        or not stat.S_ISREG(executable_metadata.st_mode)
        or not os.access(executable_path, os.X_OK)
        or executable_path != Path(str(payload["executable_path"]))
        or executable_sha256 != payload["executable_sha256"]
    ):
        raise SafeE2EError("AUTHORITY_EXECUTABLE_MISMATCH")
    ffmpeg_path = Path(str(payload["ffmpeg_path"]))
    try:
        ffmpeg_metadata = ffmpeg_path.lstat()
    except OSError as exc:
        raise SafeE2EError("AUTHORITY_FFMPEG_MISMATCH") from exc
    if (
        stat.S_ISLNK(ffmpeg_metadata.st_mode)
        or not stat.S_ISREG(ffmpeg_metadata.st_mode)
        or not os.access(ffmpeg_path, os.X_OK)
        or _sha256_file(ffmpeg_path) != payload["ffmpeg_sha256"]
    ):
        raise SafeE2EError("AUTHORITY_FFMPEG_MISMATCH")
    sandbox_profile_path = Path(str(payload["sandbox_profile_path"]))
    if (
        sandbox_profile_path != root / "authority/runtime.sb"
        or _sha256_file(sandbox_profile_path) != payload["sandbox_profile_sha256"]
    ):
        raise SafeE2EError("AUTHORITY_SANDBOX_MISMATCH")
    profile, profile_path = _read_profile(Path(str(payload["candidate_app"])))
    if (
        _sha256_file(profile_path) != payload["build_profile_sha256"]
        or profile.get("source_commit") != payload["source_commit"]
        or profile.get("source_tree") != payload["source_tree"]
    ):
        raise SafeE2EError("AUTHORITY_PROFILE_MISMATCH")
    model_root = Path(str(payload["model_root"]))
    input_path = Path(str(payload["input_path"]))
    try:
        model_root.relative_to(root)
        input_path.relative_to(root)
    except ValueError as exc:
        raise SafeE2EError("AUTHORITY_PATH_ESCAPE") from exc
    if _tree_digest(model_root) != payload["model_tree_sha256"]:
        raise SafeE2EError("AUTHORITY_MODEL_MISMATCH")
    if _sha256_file(input_path) != payload["input_sha256"]:
        raise SafeE2EError("AUTHORITY_INPUT_MISMATCH")
    receipt = SafeAuthorityReceipt(payload, raw, _SAFE_AUTHORITY_SEAL)
    _validate_test_ticket(receipt)
    return receipt


def cleanup_isolated_root(receipt: SafeAuthorityReceipt) -> dict[str, object]:
    """Remove only the exact opaque-authorized run root after revalidation."""

    if not isinstance(receipt, SafeAuthorityReceipt) or receipt._seal is not _SAFE_AUTHORITY_SEAL:
        raise SafeE2EError("AUTHORITY_RECEIPT_REQUIRED")
    root = Path(str(receipt["run_root"]))
    try:
        int(root.name, 16)
    except (TypeError, ValueError) as exc:
        raise SafeE2EError("CLEANUP_ROOT_INVALID") from exc
    if (
        len(root.name) != 32
        or root.parts[:3] != ("/", "private", "tmp")
        or len(root.parts) < 5
    ):
        raise SafeE2EError("CLEANUP_ROOT_INVALID")
    metadata = _private_directory(root)
    if (metadata.st_dev, metadata.st_ino) != (receipt["root_dev"], receipt["root_ino"]):
        raise SafeE2EError("CLEANUP_ROOT_IDENTITY_MISMATCH")
    if _tree_digest(Path(str(receipt["model_root"]))) != receipt["model_tree_sha256"]:
        raise SafeE2EError("CLEANUP_MODEL_MISMATCH")
    if _sha256_file(Path(str(receipt["input_path"]))) != receipt["input_sha256"]:
        raise SafeE2EError("CLEANUP_INPUT_MISMATCH")
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        for name in (*directories, *files):
            path = Path(current) / name
            entry = path.lstat()
            if (
                stat.S_ISLNK(entry.st_mode)
                or entry.st_uid != os.getuid()
                or entry.st_mode & 0o022
                or not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode))
            ):
                raise SafeE2EError("CLEANUP_TREE_UNSAFE")
    try:
        shutil.rmtree(root)
    except OSError as exc:
        raise SafeE2EError("CLEANUP_FAILED") from exc
    if root.exists() or root.is_symlink():
        raise SafeE2EError("CLEANUP_INCOMPLETE")
    return {
        "schema": "lulu.trusted-test.safe-cleanup-receipt/v1",
        "status": "removed",
        "root_inode": receipt["root_ino"],
    }


class _SafeAnchorStore:
    def __init__(self, store: FileCredentialStore) -> None:
        self._store = store

    def hmac_key(self) -> bytes:
        value = self._store.read(SAFE_CREDENTIAL_ACCOUNTS[1])
        try:
            key = base64.b64decode(value or "", validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SafeE2EError("JOB_HMAC_KEY_INVALID") from exc
        if len(key) < 32:
            raise SafeE2EError("JOB_HMAC_KEY_INVALID")
        return key

    def load(self) -> dict[str, object] | None:
        raw = self._store.read(SAFE_CREDENTIAL_ACCOUNTS[2])
        try:
            value = json.loads(raw or "")
        except Exception as exc:  # noqa: BLE001
            raise SafeE2EError("JOB_ANCHOR_INVALID") from exc
        if value is not None and type(value) is not dict:
            raise SafeE2EError("JOB_ANCHOR_INVALID")
        return value

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None:
        current = self.load()
        if current != expected:
            raise SafeE2EError("JOB_ANCHOR_COMPARE_AND_SET_FAILED")
        self._store.replace(
            SAFE_CREDENTIAL_ACCOUNTS[2],
            json.dumps(current, sort_keys=True, separators=(",", ":")),
            json.dumps(replacement, sort_keys=True, separators=(",", ":")),
        )


@dataclass(frozen=True)
class _SafeLicenseStatus:
    allowed: bool
    code: str
    message: str


class _SafeLicenseProjection:
    def __init__(self, receipt: SafeAuthorityReceipt, store: FileCredentialStore, now_epoch: Callable[[], int]) -> None:
        self._receipt = receipt
        self._store = store
        self._now_epoch = now_epoch

    def cached_status_projection(self):
        _validate_test_ticket(self._receipt)
        try:
            payload = json.loads(self._store.read(SAFE_CREDENTIAL_ACCOUNTS[0]) or "")
        except Exception as exc:  # noqa: BLE001
            raise SafeE2EError("SAFE_LICENSE_INVALID") from exc
        allowed = (
            type(payload) is dict
            and payload.get("allowed") is True
            and type(payload.get("expires_at")) is int
            and self._now_epoch() <= payload["expires_at"]
        )
        return _SafeLicenseStatus(
            bool(allowed),
            "safe_test_allowed" if allowed else "expired",
            "Trusted Test isolated authorization",
        )


class _SafeApplicationMixin:
    def _assert_model_ready(self) -> None:
        from .core.transcription_job_application import LocalTranscriptionJobError

        try:
            ready = bool(self._model_readiness_probe("local_sensevoice"))
        except LocalTranscriptionJobError:
            raise
        except Exception as exc:  # noqa: BLE001 - safe injected boundary
            raise LocalTranscriptionJobError(
                "RUNTIME_NOT_READY",
                "MODEL_READINESS_PROBE_FAILED",
            ) from exc
        if not ready:
            raise LocalTranscriptionJobError("MODEL_NOT_READY", "MODEL_NOT_READY")

    def _assert_verified_trusted_test_runtime(self) -> str:
        verification = self._runtime_verification
        installation_id = verification.get("installation_id")
        if (
            verification.get("profile") != "trusted_test_safe_e2e"
            or verification.get("safe_test_ticket_verified") is not True
            or type(installation_id) is not str
            or not installation_id.startswith("safe-")
        ):
            from .core.transcription_job_application import LocalTranscriptionJobError

            raise LocalTranscriptionJobError("CAPABILITY_UNAVAILABLE", "SAFE_TEST_AUTHORITY_REQUIRED")
        return installation_id


class SafeLocalRuntime:
    def __init__(self, application, job_control, store, now: Callable[[], str]) -> None:
        self._application = application
        self._job_control = job_control
        self._store = store
        self._now = now

    def run(self, source_path: str) -> dict[str, object]:
        return self._application.run(source_path)

    def jobs(self) -> tuple[object, ...]:
        self._application.assert_job_control_allowed("JOB-01")
        return self._job_control.list()

    def recover_and_run(self, source_path: str) -> dict[str, object]:
        from .core.job_ledger.contracts import JobState
        from .core.transcription_contracts import LocalFileTranscriptionRequestV1

        self._application._preflight(source_path)
        self._store.recover_orphaned_running(changed_at=self._now())
        recoverable = [
            job
            for job in self._job_control.list()
            if job.state is JobState.RECOVERY_REQUIRED
        ]
        if len(recoverable) != 1:
            raise SafeE2EError("RECOVERABLE_JOB_REQUIRED")
        job = recoverable[0]
        source_summary = self._application._read_source_summary(source_path)
        if job.input_ref != f"source:sha256:{source_summary['sha256']}":
            raise SafeE2EError("RECOVERY_SOURCE_MISMATCH")
        queued = self._job_control.resume(
            job.job_id,
            expected_revision=job.job_revision,
            changed_at=self._now(),
        )
        running, lease = self._job_control.claim_worker(
            job.job_id,
            expected_revision=queued.job_revision,
            worker_instance_id=f"safe-e2e-recovery:{job.job_id}",
            operation_receipt=None,
            changed_at=self._now(),
        )
        try:
            result = self._application._transcription_service.transcribe(
                LocalFileTranscriptionRequestV1(source_path=source_path),
                is_cancelled=lambda: False,
            )
            stored = dict(self._application._result_store.write(job.job_id, result))
        except Exception:
            self._job_control.complete_worker(
                job.job_id,
                expected_revision=running.job_revision,
                lease_token=lease,
                new_state=JobState.FAILED,
                result_ref=None,
                changed_at=self._now(),
            )
            raise
        result_ref = stored.get("result_ref")
        content_hash = stored.get("content_hash")
        if type(result_ref) is not str or type(content_hash) is not str or len(content_hash) != 64:
            raise SafeE2EError("RECOVERY_RESULT_RECEIPT_INVALID")
        completed = self._job_control.complete_worker(
            job.job_id,
            expected_revision=running.job_revision,
            lease_token=lease,
            new_state=JobState.SUCCEEDED,
            result_ref=result_ref,
            changed_at=self._now(),
        )
        return {
            "status": completed.state.value,
            "result_ref": result_ref,
            "content_hash": content_hash,
            "job_receipt": {
                "job_id": completed.job_id,
                "job_revision": completed.job_revision,
                "state": completed.state.value,
                "last_sequence": completed.event_sequence,
                "result_ref": completed.result_ref,
                "receipt_ref": f"lulu-job:{completed.job_id}:revision:{completed.job_revision}",
            },
        }


def build_safe_runtime(
    receipt: SafeAuthorityReceipt,
    *,
    pipeline_runner=None,
    model_readiness_probe=None,
    now_epoch: Callable[[], int] | None = None,
) -> SafeLocalRuntime:
    if not isinstance(receipt, SafeAuthorityReceipt) or receipt._seal is not _SAFE_AUTHORITY_SEAL:
        raise SafeE2EError("AUTHORITY_RECEIPT_REQUIRED")
    store = _validate_test_ticket(receipt)
    clock = now_epoch or (lambda: int(time.time()))
    for key, value in isolated_environment(Path(str(receipt["run_root"]))).items():
        os.environ[key] = value

    from .core.auth_guard import UnifiedLicenseAuthGuard
    from .core.job_ledger.service import JobControlService
    from .core.job_ledger.store import JobLedgerStore
    from .core.storage_layout import SharedProductStorageLayout
    from .core.transcription_application import LocalFileTranscriptionApplicationService
    from .core.transcription_job_application import LocalTranscriptionJobApplication
    from .core.transcription_result_store import TranscriptionResultStore

    projection = _SafeLicenseProjection(receipt, store, clock)
    guard = UnifiedLicenseAuthGuard(projection)
    layout = SharedProductStorageLayout.from_application_support_root(
        Path(str(receipt["run_root"])) / "home/Library/Application Support"
    )
    anchor = _SafeAnchorStore(store)
    job_store = JobLedgerStore(layout.jobs, anchor)
    result_store = TranscriptionResultStore(layout)
    readiness = model_readiness_probe or _authority_bound_model_readiness(receipt)
    runner = pipeline_runner or _authority_bound_pipeline_runner(
        Path(str(receipt["run_root"])),
        readiness,
    )
    service = LocalFileTranscriptionApplicationService(
        guard,
        pipeline_runner=runner,
        model_readiness_probe=readiness,
    )
    application_type = type(
        "SafeLocalTranscriptionJobApplication",
        (_SafeApplicationMixin, LocalTranscriptionJobApplication),
        {},
    )
    iso_now = lambda: datetime.fromtimestamp(clock(), timezone.utc).isoformat().replace("+00:00", "Z")
    application = application_type(
        runtime_verification={
            "profile": "trusted_test_safe_e2e",
            "verified": True,
            "safe_test_ticket_verified": True,
            "installation_id": receipt["installation_id"],
        },
        authorization_guard=guard,
        model_readiness_probe=readiness,
        transcription_service=service,
        job_store=job_store,
        result_store=result_store,
        id_factory=lambda kind: f"{kind}-{secrets.token_hex(16)}",
        now=iso_now,
    )
    return SafeLocalRuntime(
        application,
        JobControlService(job_store, enforce_fencing=True),
        job_store,
        iso_now,
    )


def _frozen_safe_executable() -> Path:
    if not bool(getattr(sys, "frozen", False)):
        raise SafeE2EError("FROZEN_SAFE_TEST_ENTRY_REQUIRED")
    executable = Path(str(getattr(sys, "executable", "")))
    if (
        not executable.is_absolute()
        or executable.name != "Lulu-Trusted-Test-Safe-E2E"
        or executable.parent.name != "MacOS"
        or executable.parent.parent.name != "Contents"
        or _sha256_file(executable) == ""
    ):
        raise SafeE2EError("FROZEN_SAFE_TEST_ENTRY_INVALID")
    return executable


def _job_public(job: object) -> dict[str, object]:
    attempt = getattr(job, "attempts", ())[-1]
    return {
        "job_id": getattr(job, "job_id"),
        "job_revision": getattr(job, "job_revision"),
        "state": getattr(getattr(job, "state"), "value"),
        "last_sequence": getattr(job, "event_sequence"),
        "lease_epoch": getattr(attempt, "lease_epoch"),
        "result_ref": getattr(job, "result_ref"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--authority", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--pause-before-inference", action="store_true")
    subparsers.add_parser("recover")
    subparsers.add_parser("jobs")
    subparsers.add_parser("doctor")
    subparsers.add_parser("version")
    subparsers.add_parser("capabilities")
    return parser


def _pause_pipeline(run_root: Path):
    def runner(source_path, *, engine_name, progress, is_cancelled):
        marker = run_root / "receipts/running-ready.json"
        _write_private_json(
            marker,
            {
                "schema": "lulu.trusted-test.safe-running-ready/v1",
                "pid": os.getpid(),
                "source_sha256": _sha256_file(Path(source_path)),
            },
        )
        continue_marker = run_root / "receipts/continue"
        while not continue_marker.exists():
            if is_cancelled():
                raise SafeE2EError("SAFE_TEST_CANCELLED")
            time.sleep(0.05)
        from .core.transcription_application import run_local_transcription_pipeline_adapter

        return run_local_transcription_pipeline_adapter(
            source_path,
            engine_name=engine_name,
            progress=progress,
            is_cancelled=is_cancelled,
        )

    return runner


def _emit(payload: Mapping[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def _known_product_error(exc: Exception) -> dict[str, object] | None:
    try:
        from .core.transcription_application import LocalFileTranscriptionError
        from .core.transcription_job_application import LocalTranscriptionJobError
    except Exception:
        return None
    if isinstance(exc, LocalTranscriptionJobError):
        reason = exc.reason_code
        stage = _SAFE_JOB_ERROR_STAGES.get(reason)
    elif isinstance(exc, LocalFileTranscriptionError):
        reason = exc.failure.code
        stage = _SAFE_SERVICE_ERROR_STAGES.get(reason)
    else:
        return None
    if stage is None:
        return None
    return {
        "ok": False,
        "error": "SAFE_E2E_KNOWN_ERROR",
        "stage": stage,
        "reason": reason,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        executable = _frozen_safe_executable()
        arguments = _parser().parse_args(argv)
        authority_path = Path(arguments.authority)
        receipt = load_safe_authority(
            authority_path,
            executable_path=executable,
        )
        run_root = Path(str(receipt["run_root"]))
        for key, value in isolated_environment(run_root).items():
            os.environ[key] = value
        install_runtime_guards(
            run_root,
            allowed_subprocesses={
                Path(str(receipt["ffmpeg_path"])): str(receipt["ffmpeg_sha256"]),
            },
        )
        command = arguments.command
        if command == "version":
            from .version import __build__, __version__

            _emit({"ok": True, "profile": "trusted_test_safe_e2e", "version": __version__, "build": __build__})
            return 0
        if command == "capabilities":
            _emit({"ok": True, "capabilities": ["local_file_transcription", "jobs_read", "orphan_recovery"]})
            return 0
        if command == "doctor":
            _emit(
                {
                    "ok": True,
                    "authority": "verified",
                    "network": "denied",
                    "credential_backend": "isolated_file",
                    "model_tree_sha256": receipt["model_tree_sha256"],
                }
            )
            return 0
        pipeline_runner = (
            _pause_pipeline(run_root)
            if bool(getattr(arguments, "pause_before_inference", False))
            else None
        )
        runtime = build_safe_runtime(receipt, pipeline_runner=pipeline_runner)
        if command == "run":
            result = runtime.run(str(receipt["input_path"]))
        elif command == "recover":
            result = runtime.recover_and_run(str(receipt["input_path"]))
        elif command == "jobs":
            result = {"jobs": [_job_public(job) for job in runtime.jobs()]}
        else:
            raise SafeE2EError("SAFE_COMMAND_FORBIDDEN")
        _emit({"ok": True, **result})
        return 0
    except SystemExit:
        raise
    except SafeE2EError as exc:
        _emit({"ok": False, "error": exc.reason_code})
        return 64
    except Exception as exc:
        known = _known_product_error(exc)
        if known is not None:
            _emit(known)
            return 65
        _emit({"ok": False, "error": "SAFE_E2E_INTERNAL_ERROR"})
        return 70
