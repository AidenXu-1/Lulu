"""Phase 0A contract for the retired historical CLI entry point."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


# Frozen literals come from ``legacy_cli_contract`` in the machine contract,
# independently of the production implementation under test.
EXPECTED_STDOUT = ""
EXPECTED_STDERR = "LEGACY_CLI_DISABLED\n"
EXPECTED_EXIT = 2
SECRET_SENTINEL = "PHASE0A_SECRET_SENTINEL"
PIPE_SECRET_SENTINEL = "PHASE0A_PIPE_SECRET_SENTINEL"
SECRET_ENVIRONMENT = {
    "LULU_PHASE0A_API_KEY": SECRET_SENTINEL,
    "LULU_PHASE0A_TOKEN": "PHASE0A_TOKEN_SENTINEL",
}

APP_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
SRC_INIT = APP_ROOT / "src" / "__init__.py"
CLI_SOURCE = APP_ROOT / "src" / "cli.py"
FORBIDDEN_BUSINESS_MODULE_PREFIXES = (
    "src.core",
    "PySide6",
    "keyring",
    "torch",
    "funasr",
)


class Phase0AForbiddenOperation(RuntimeError):
    """Stop a forbidden operation before it can create a real side effect."""


class Phase0ABusinessReadOracle:
    """Classify frozen import-loader reads and reject every business read."""

    INSTRUMENTED_CALLS = (
        "builtins.open",
        "io.open",
        "os.open",
        "os.stat",
        "os.lstat",
        "pathlib.Path.open",
        "pathlib.Path.read_text",
        "pathlib.Path.read_bytes",
        "pathlib.Path.stat",
        "pathlib.Path.lstat",
    )

    _IMPORT_MODULES = {
        "_frozen_importlib",
        "_frozen_importlib_external",
        "importlib",
        "runpy",
        "site",
        "zipimport",
    }
    _IMPORT_PREFIXES = ("importlib.", "encodings.")

    def __init__(
        self,
        sentinel_paths: tuple[Path, Path],
        *,
        hook_path: Path | None = None,
        report_fd: int | None = None,
    ) -> None:
        self.business_reads: list[str] = []
        self._sentinels = {self._normalize_path(path) for path in sentinel_paths}
        self._report_fd = report_fd
        self._patchers: list[mock._patch] = []
        self._oracle_modules = {__name__, "sitecustomize"}

        sources = {SRC_INIT, CLI_SOURCE}
        if hook_path is not None:
            sources.add(hook_path)
        allowed = {self._normalize_path(path) for path in sources}
        for source in sources:
            try:
                allowed.add(self._normalize_path(importlib.util.cache_from_source(str(source))))
            except (NotImplementedError, ValueError):
                pass
        self._allowed_project_imports = allowed
        self._python_prefixes = {
            self._normalize_path(sys.base_prefix),
            self._normalize_path(sys.exec_prefix),
        }

    @staticmethod
    def _normalize_path(target: object) -> str:
        if isinstance(target, int):
            return f"<fd:{target}>"
        value = os.fsdecode(os.fspath(target))
        return os.path.normcase(os.path.abspath(os.path.normpath(value)))

    @staticmethod
    def _is_write_mode(mode: object) -> bool:
        return any(marker in str(mode) for marker in ("w", "a", "x", "+"))

    @staticmethod
    def _is_write_flags(flags: int) -> bool:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & write_flags)

    def _under_python_prefix(self, target: str) -> bool:
        for prefix in self._python_prefixes:
            try:
                if os.path.commonpath((target, prefix)) == prefix:
                    return True
            except ValueError:
                continue
        return False

    def _has_import_loader_stack(self) -> bool:
        frame = sys._getframe(2)
        while frame is not None:
            module_name = str(frame.f_globals.get("__name__", ""))
            if module_name in self._oracle_modules:
                frame = frame.f_back
                continue
            if module_name == __name__:
                # The direct test harness is outside the application stack.
                return True
            if module_name in self._IMPORT_MODULES or module_name.startswith(
                self._IMPORT_PREFIXES
            ):
                frame = frame.f_back
                continue
            return False
        return True

    def _is_import_loader_read(self, target: str, *, is_write: bool) -> bool:
        if is_write or target in self._sentinels:
            return False
        target_allowed = target in self._allowed_project_imports or self._under_python_prefix(
            target
        )
        return target_allowed and self._has_import_loader_stack()

    def _report(self, category: str, operation: str, target: str) -> None:
        detail = f"{category}\t{operation}\t{target}"
        if category == "BUSINESS_READ":
            self.business_reads.append(detail)
        if self._report_fd is not None:
            os.write(self._report_fd, f"{detail}\n".encode("utf-8", "backslashreplace"))

    def _check(self, operation: str, target: object, *, is_write: bool = False) -> None:
        normalized = self._normalize_path(target)
        if self._is_import_loader_read(normalized, is_write=is_write):
            return
        self._report("BUSINESS_READ", operation, normalized)
        raise Phase0AForbiddenOperation(
            f"Phase 0A forbids {operation} on business target {normalized}"
        )

    def start(self) -> None:
        originals = {
            "builtins.open": builtins.open,
            "io.open": io.open,
            "os.open": os.open,
            "os.stat": os.stat,
            "os.lstat": os.lstat,
            "Path.open": Path.open,
            "Path.read_text": Path.read_text,
            "Path.read_bytes": Path.read_bytes,
            "Path.stat": Path.stat,
            "Path.lstat": Path.lstat,
        }

        def guarded_builtin_open(file, mode="r", *args, **kwargs):
            self._check("builtins.open", file, is_write=self._is_write_mode(mode))
            return originals["builtins.open"](file, mode, *args, **kwargs)

        def guarded_io_open(file, mode="r", *args, **kwargs):
            self._check("io.open", file, is_write=self._is_write_mode(mode))
            return originals["io.open"](file, mode, *args, **kwargs)

        def guarded_os_open(path, flags, *args, **kwargs):
            self._check("os.open", path, is_write=self._is_write_flags(flags))
            return originals["os.open"](path, flags, *args, **kwargs)

        def guarded_os_stat(path, *args, **kwargs):
            self._check("os.stat", path)
            return originals["os.stat"](path, *args, **kwargs)

        def guarded_os_lstat(path, *args, **kwargs):
            self._check("os.lstat", path)
            return originals["os.lstat"](path, *args, **kwargs)

        def guarded_path_open(path, mode="r", *args, **kwargs):
            self._check("pathlib.Path.open", path, is_write=self._is_write_mode(mode))
            return originals["Path.open"](path, mode, *args, **kwargs)

        def guarded_path_read_text(path, *args, **kwargs):
            self._check("pathlib.Path.read_text", path)
            return originals["Path.read_text"](path, *args, **kwargs)

        def guarded_path_read_bytes(path, *args, **kwargs):
            self._check("pathlib.Path.read_bytes", path)
            return originals["Path.read_bytes"](path, *args, **kwargs)

        def guarded_path_stat(path, *args, **kwargs):
            self._check("pathlib.Path.stat", path)
            return originals["Path.stat"](path, *args, **kwargs)

        def guarded_path_lstat(path, *args, **kwargs):
            self._check("pathlib.Path.lstat", path)
            return originals["Path.lstat"](path, *args, **kwargs)

        patches = (
            mock.patch.object(builtins, "open", guarded_builtin_open),
            mock.patch.object(io, "open", guarded_io_open),
            mock.patch.object(os, "open", guarded_os_open),
            mock.patch.object(os, "stat", guarded_os_stat),
            mock.patch.object(os, "lstat", guarded_os_lstat),
            mock.patch.object(Path, "open", guarded_path_open),
            mock.patch.object(Path, "read_text", guarded_path_read_text),
            mock.patch.object(Path, "read_bytes", guarded_path_read_bytes),
            mock.patch.object(Path, "stat", guarded_path_stat),
            mock.patch.object(Path, "lstat", guarded_path_lstat),
        )
        for patcher in patches:
            patcher.start()
            self._patchers.append(patcher)

    def stop(self) -> None:
        while self._patchers:
            self._patchers.pop().stop()


class Phase0ASideEffectGuard:
    """Reject frozen filesystem, temp, network, DNS and subprocess effects."""

    def __init__(self, *, report_fd: int | None = None) -> None:
        self.effects: list[str] = []
        self._report_fd = report_fd
        self._patchers: list[mock._patch] = []

    def _forbidden(self, category: str, operation: str):
        def guard(*_args, **_kwargs):
            detail = f"SIDE_EFFECT\t{category}\t{operation}"
            self.effects.append(detail)
            if self._report_fd is not None:
                os.write(self._report_fd, f"{detail}\n".encode("utf-8"))
            raise Phase0AForbiddenOperation(f"Phase 0A forbids {category}: {operation}")

        return guard

    def start(self) -> None:
        targets = (
            (tempfile, "mkstemp", "TEMP"),
            (tempfile, "mkdtemp", "TEMP"),
            (tempfile, "NamedTemporaryFile", "TEMP"),
            (tempfile, "TemporaryFile", "TEMP"),
            (tempfile, "TemporaryDirectory", "TEMP"),
            (os, "mkdir", "FILESYSTEM_MUTATION"),
            (os, "makedirs", "FILESYSTEM_MUTATION"),
            (os, "remove", "FILESYSTEM_MUTATION"),
            (os, "unlink", "FILESYSTEM_MUTATION"),
            (os, "rename", "FILESYSTEM_MUTATION"),
            (os, "replace", "FILESYSTEM_MUTATION"),
            (os, "system", "SUBPROCESS"),
            (Path, "write_text", "FILESYSTEM_MUTATION"),
            (Path, "write_bytes", "FILESYSTEM_MUTATION"),
            (Path, "mkdir", "FILESYSTEM_MUTATION"),
            (Path, "touch", "FILESYSTEM_MUTATION"),
            (Path, "unlink", "FILESYSTEM_MUTATION"),
            (Path, "rename", "FILESYSTEM_MUTATION"),
            (Path, "replace", "FILESYSTEM_MUTATION"),
            (subprocess, "Popen", "SUBPROCESS"),
            (socket, "socket", "NETWORK"),
            (socket, "create_connection", "NETWORK"),
            (socket, "getaddrinfo", "DNS"),
            (socket, "gethostbyname", "DNS"),
        )
        for owner, attribute, category in targets:
            patcher = mock.patch.object(
                owner,
                attribute,
                self._forbidden(category, f"{owner.__name__}.{attribute}"),
            )
            patcher.start()
            self._patchers.append(patcher)

    def stop(self) -> None:
        while self._patchers:
            self._patchers.pop().stop()


class Phase0ASecretEnvironmentGuard:
    """Reject reads of the exact secret-like environment keys in the fixture."""

    def __init__(self, *, report_fd: int | None = None) -> None:
        self.reads: list[str] = []
        self._report_fd = report_fd
        self._patchers: list[mock._patch] = []

    def _record(self, key: object) -> None:
        normalized = str(key)
        if normalized not in SECRET_ENVIRONMENT:
            return
        detail = f"SECRET_ENV_READ\t{normalized}"
        self.reads.append(detail)
        if self._report_fd is not None:
            os.write(self._report_fd, f"{detail}\n".encode("utf-8"))
        raise Phase0AForbiddenOperation(f"Phase 0A forbids reading secret env key {normalized}")

    def start(self) -> None:
        environ_type = type(os.environ)
        original_getitem = environ_type.__getitem__
        original_get = environ_type.get
        original_contains = environ_type.__contains__
        original_getenv = os.getenv

        def guarded_getitem(environ, key):
            self._record(key)
            return original_getitem(environ, key)

        def guarded_get(environ, key, default=None):
            self._record(key)
            return original_get(environ, key, default)

        def guarded_contains(environ, key):
            self._record(key)
            return original_contains(environ, key)

        def guarded_getenv(key, default=None):
            self._record(key)
            return original_getenv(key, default)

        patches = (
            mock.patch.object(environ_type, "__getitem__", guarded_getitem),
            mock.patch.object(environ_type, "get", guarded_get),
            mock.patch.object(environ_type, "__contains__", guarded_contains),
            mock.patch.object(os, "getenv", guarded_getenv),
        )
        for patcher in patches:
            patcher.start()
            self._patchers.append(patcher)

    def stop(self) -> None:
        while self._patchers:
            self._patchers.pop().stop()


class RaisingStdin:
    """A stdin replacement that reports every read or poll attempt."""

    def __init__(self, report_fd: int | None = None) -> None:
        self.reads: list[str] = []
        self._report_fd = report_fd

    @property
    def buffer(self) -> RaisingStdin:
        return self

    def _forbid(self, operation: str):
        detail = f"STDIN_READ\t{operation}"
        self.reads.append(detail)
        if self._report_fd is not None:
            os.write(self._report_fd, f"{detail}\n".encode("utf-8"))
        raise Phase0AForbiddenOperation(f"Phase 0A forbids stdin operation {operation}")

    def read(self, *_args, **_kwargs):
        self._forbid("read")

    def readline(self, *_args, **_kwargs):
        self._forbid("readline")

    def readlines(self, *_args, **_kwargs):
        self._forbid("readlines")

    def fileno(self):
        self._forbid("fileno")

    def isatty(self):
        self._forbid("isatty")

    def __iter__(self):
        self._forbid("iter")


def _owned_tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    snapshot: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", b""))
        elif path.is_file():
            snapshot.append((relative, "file", path.read_bytes()))
        else:
            snapshot.append((relative, "other", b""))
    return tuple(snapshot)


def _project_modules() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "src" or name.startswith("src.")
    }


def _restore_project_modules(before: dict[str, object]) -> None:
    for name in list(sys.modules):
        if (name == "src" or name.startswith("src.")) and name not in before:
            sys.modules.pop(name, None)
    sys.modules.update(before)


class LegacyCliDisabledContractTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        source = root / "phase0a-must-not-read-source.wav"
        output = root / "phase0a-must-not-stat-output.md"
        hook_dir = root / "hook"
        hook_dir.mkdir()
        source.write_bytes(b"PHASE0A_SOURCE_SENTINEL")
        return source, output, hook_dir

    def _argv_cases(self, source: Path, output: Path) -> tuple[tuple[str, list[str]], ...]:
        return (
            ("no arguments", []),
            ("local source", [str(source)]),
            ("url source", ["https://example.invalid/phase0a"]),
            (
                "legacy output and engine",
                [str(source), "--output", str(output), "--engine", "local_sensevoice"],
            ),
            ("legacy api key", [str(source), "--api-key", SECRET_SENTINEL]),
            ("unknown argument", ["--phase0a-unknown"]),
        )

    def _assert_fixed_result(self, *, exit_code: int, stdout: str, stderr: str) -> None:
        self.assertEqual(exit_code, EXPECTED_EXIT)
        self.assertEqual(stdout, EXPECTED_STDOUT)
        self.assertEqual(stderr, EXPECTED_STDERR)
        self.assertNotIn(SECRET_SENTINEL, stdout + stderr)
        self.assertNotIn(PIPE_SECRET_SENTINEL, stdout + stderr)

    def _write_sitecustomize(self, hook_dir: Path) -> Path:
        hook_path = hook_dir / "sitecustomize.py"
        module_name = Path(__file__).stem
        hook_path.write_text(
            f"""\
import atexit
import os
import sys
from {module_name} import (
    FORBIDDEN_BUSINESS_MODULE_PREFIXES,
    Phase0ABusinessReadOracle,
    Phase0ASecretEnvironmentGuard,
    Phase0ASideEffectGuard,
    RaisingStdin,
)

_report_fd = int(os.environ["PHASE0A_ORACLE_FD"])
_source = os.environ["PHASE0A_SENTINEL_SOURCE"]
_output = os.environ["PHASE0A_SENTINEL_OUTPUT"]
_oracle = Phase0ABusinessReadOracle(
    (__import__("pathlib").Path(_source), __import__("pathlib").Path(_output)),
    hook_path=__import__("pathlib").Path(__file__),
    report_fd=_report_fd,
)
_effects = Phase0ASideEffectGuard(report_fd=_report_fd)
_environment = Phase0ASecretEnvironmentGuard(report_fd=_report_fd)
_oracle.start()
_effects.start()
_environment.start()
sys.stdin = RaisingStdin(_report_fd)
sys.dont_write_bytecode = True

def _report_business_imports():
    for name in sorted(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_BUSINESS_MODULE_PREFIXES):
            os.write(_report_fd, f"BUSINESS_IMPORT\\t{{name}}\\n".encode("utf-8"))

atexit.register(_report_business_imports)
os.write(_report_fd, b"ARMED\\n")
""",
            encoding="utf-8",
        )
        return hook_path

    def test_direct_main_is_fixed_fail_closed_for_every_input(self):
        with tempfile.TemporaryDirectory(prefix="lulu-phase0a-direct-") as temp_root:
            root = Path(temp_root)
            source, output, _hook_dir = self._fixture(root)
            before_tree = _owned_tree_snapshot(root)
            modules_before = _project_modules()
            previous_dont_write_bytecode = sys.dont_write_bytecode
            previous_stdin = sys.stdin
            oracle = Phase0ABusinessReadOracle((source, output))
            effects = Phase0ASideEffectGuard()
            environment = Phase0ASecretEnvironmentGuard()
            raising_stdin = RaisingStdin()
            import_error: BaseException | None = None
            results: list[tuple[str, int | None, str, str, BaseException | None]] = []

            try:
                sys.dont_write_bytecode = True
                with mock.patch.dict(os.environ, SECRET_ENVIRONMENT, clear=False):
                    oracle.start()
                    effects.start()
                    environment.start()
                    sys.stdin = raising_stdin
                    try:
                        sys.modules.pop("src.cli", None)
                        try:
                            cli = importlib.import_module("src.cli")
                        except BaseException as exc:  # recorded and asserted below
                            import_error = exc
                            cli = None

                        if cli is not None:
                            for label, argv in self._argv_cases(source, output):
                                stdout = io.StringIO()
                                stderr = io.StringIO()
                                exit_code: int | None = None
                                execution_error: BaseException | None = None
                                try:
                                    with redirect_stdout(stdout), redirect_stderr(stderr):
                                        exit_code = cli.main(argv)
                                except BaseException as exc:  # recorded and asserted below
                                    execution_error = exc
                                results.append(
                                    (
                                        label,
                                        exit_code,
                                        stdout.getvalue(),
                                        stderr.getvalue(),
                                        execution_error,
                                    )
                                )
                    finally:
                        environment.stop()
                        effects.stop()
                        oracle.stop()
            finally:
                sys.stdin = previous_stdin
                sys.dont_write_bytecode = previous_dont_write_bytecode
                _restore_project_modules(modules_before)

            self.assertIsNone(import_error, f"src.cli crossed a forbidden boundary: {import_error}")
            self.assertEqual(len(results), len(self._argv_cases(source, output)))
            for label, exit_code, stdout, stderr, execution_error in results:
                with self.subTest(argv_case=label):
                    self.assertIsNone(
                        execution_error,
                        f"public main(argv) crossed a forbidden boundary: {execution_error}",
                    )
                    self._assert_fixed_result(
                        exit_code=exit_code,
                        stdout=stdout,
                        stderr=stderr,
                    )
            self.assertEqual(oracle.business_reads, [])
            self.assertEqual(effects.effects, [])
            self.assertEqual(environment.reads, [])
            self.assertEqual(raising_stdin.reads, [])
            forbidden_imports = sorted(
                name
                for name in set(sys.modules) - set(modules_before)
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in FORBIDDEN_BUSINESS_MODULE_PREFIXES
                )
            )
            self.assertEqual(forbidden_imports, [])
            self.assertEqual(_owned_tree_snapshot(root), before_tree)

    def test_real_python_m_is_armed_and_fixed_fail_closed_for_every_input(self):
        with tempfile.TemporaryDirectory(prefix="lulu-phase0a-process-") as temp_root:
            root = Path(temp_root)
            source, output, hook_dir = self._fixture(root)
            hook_path = self._write_sitecustomize(hook_dir)
            self.assertEqual(hook_path.name, "sitecustomize.py")
            before_tree = _owned_tree_snapshot(root)

            for label, argv in self._argv_cases(source, output):
                with self.subTest(argv_case=label):
                    read_fd, write_fd = os.pipe()
                    os.set_inheritable(write_fd, True)
                    environment = os.environ.copy()
                    environment.update(SECRET_ENVIRONMENT)
                    environment.update(
                        {
                            "PHASE0A_ORACLE_FD": str(write_fd),
                            "PHASE0A_SENTINEL_SOURCE": str(source),
                            "PHASE0A_SENTINEL_OUTPUT": str(output),
                            "PYTHONPATH": os.pathsep.join(
                                (str(hook_dir), str(TESTS_ROOT), str(APP_ROOT))
                            ),
                        }
                    )
                    try:
                        completed = subprocess.run(
                            [sys.executable, "-B", "-m", "src.cli", *argv],
                            cwd=APP_ROOT,
                            env=environment,
                            input=PIPE_SECRET_SENTINEL.encode("utf-8"),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            pass_fds=(write_fd,),
                            check=False,
                            timeout=15,
                        )
                    finally:
                        os.close(write_fd)

                    report_chunks: list[bytes] = []
                    try:
                        while True:
                            chunk = os.read(read_fd, 65536)
                            if not chunk:
                                break
                            report_chunks.append(chunk)
                    finally:
                        os.close(read_fd)
                    report_lines = b"".join(report_chunks).decode("utf-8").splitlines()

                    self.assertEqual(report_lines, ["ARMED"])
                    self._assert_fixed_result(
                        exit_code=completed.returncode,
                        stdout=completed.stdout.decode("utf-8"),
                        stderr=completed.stderr.decode("utf-8"),
                    )
                    self.assertEqual(_owned_tree_snapshot(root), before_tree)


if __name__ == "__main__":
    unittest.main()
