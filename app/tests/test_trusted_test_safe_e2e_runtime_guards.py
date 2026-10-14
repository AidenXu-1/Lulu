from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


APP_ROOT = Path(__file__).resolve().parents[1]


class TrustedTestSafeE2ERuntimeGuardTests(unittest.TestCase):
    def _run_probe(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-c", body],
            cwd=APP_ROOT,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(APP_ROOT)},
        )

    def test_audit_guard_denies_outside_write_before_creation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            inside = root / "inside.txt"
            outside = Path(temp) / "outside" / "escape.txt"
            body = f"""
import os, pathlib, sys
sys.path.insert(0, {str(APP_ROOT)!r})
from src.trusted_test_safe_e2e import install_runtime_guards, SafeE2EError
root = pathlib.Path({str(root)!r})
install_runtime_guards(root)
pathlib.Path({str(inside)!r}).write_text('ok', encoding='utf-8')
try:
    pathlib.Path({str(outside)!r}).parent.mkdir()
except SafeE2EError as exc:
    print(exc.reason_code)
else:
    raise SystemExit(9)
"""
            result = self._run_probe(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WRITE_OUTSIDE_ROOT", result.stdout)
            self.assertEqual(inside.read_text(encoding="utf-8"), "ok")
            self.assertFalse(outside.parent.exists())

    def test_guard_denies_network_keyring_and_authorization_tools(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            spoof = Path(temp) / "ffmpeg-spoof"
            spoof.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(spoof, 0o700)
            body = f"""
import pathlib, socket, subprocess, sys
sys.path.insert(0, {str(APP_ROOT)!r})
from src.trusted_test_safe_e2e import install_runtime_guards, SafeE2EError
install_runtime_guards(pathlib.Path({str(root)!r}))
reasons = []
for probe in (
    lambda: socket.create_connection(('127.0.0.1', 9), timeout=0.01),
    lambda: __import__('keyring'),
    lambda: subprocess.run(['/usr/bin/security', 'help']),
    lambda: subprocess.run(['/usr/bin/codesign', '--help']),
    lambda: subprocess.run([{str(spoof)!r}]),
):
    try:
        probe()
    except (SafeE2EError, ImportError) as exc:
        reasons.append(getattr(exc, 'reason_code', str(exc)))
    else:
        raise SystemExit(8)
print('|'.join(reasons))
"""
            result = self._run_probe(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NETWORK_FORBIDDEN", result.stdout)
            self.assertIn("KEYCHAIN_MODULE_FORBIDDEN", result.stdout)
            self.assertGreaterEqual(result.stdout.count("AUTHORIZATION_TOOL_FORBIDDEN"), 3)

    def test_guard_allows_only_the_non_persistent_dev_null_write_sink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            outside = Path(temp) / "outside.txt"
            body = f"""
import os, pathlib, sys
sys.path.insert(0, {str(APP_ROOT)!r})
from src.trusted_test_safe_e2e import install_runtime_guards, SafeE2EError
install_runtime_guards(pathlib.Path({str(root)!r}))
with open(os.devnull, 'w', encoding='utf-8') as sink:
    sink.write('discarded')
try:
    pathlib.Path({str(outside)!r}).write_text('forbidden', encoding='utf-8')
except SafeE2EError as exc:
    print(exc.reason_code)
else:
    raise SystemExit(8)
"""
            result = self._run_probe(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WRITE_OUTSIDE_ROOT", result.stdout)
            self.assertFalse(outside.exists())

    def test_relative_ffmpeg_probe_is_hidden_before_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            root = base / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            tools = base / "tools"
            tools.mkdir()
            spoof = tools / "ffmpeg"
            sentinel = base / "executed"
            spoof.write_text(
                f"#!/bin/sh\ntouch {sentinel}\n",
                encoding="utf-8",
            )
            os.chmod(spoof, 0o700)
            body = f"""
import os, pathlib, subprocess, sys
sys.path.insert(0, {str(APP_ROOT)!r})
from src.trusted_test_safe_e2e import install_runtime_guards, SafeE2EError
os.environ['PATH'] = {str(tools)!r}
install_runtime_guards(pathlib.Path({str(root)!r}))
try:
    subprocess.run(['ffmpeg', '-version'], check=False)
except FileNotFoundError:
    print('RELATIVE_FFMPEG_HIDDEN')
else:
    raise SystemExit(8)
try:
    subprocess.run(['other-relative-tool'], check=False)
except SafeE2EError as exc:
    print(exc.reason_code)
else:
    raise SystemExit(9)
"""
            result = self._run_probe(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RELATIVE_FFMPEG_HIDDEN", result.stdout)
            self.assertIn("AUTHORIZATION_TOOL_FORBIDDEN", result.stdout)
            self.assertFalse(sentinel.exists())

    def test_dir_fd_relative_cleanup_is_bound_to_the_open_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            root = base / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            inside = root / "inside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside_dir = base / "outside"
            outside_dir.mkdir()
            outside = outside_dir / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            body = f"""
import os, pathlib, sys, tempfile
sys.path.insert(0, {str(APP_ROOT)!r})
from src.trusted_test_safe_e2e import install_runtime_guards, SafeE2EError
root = pathlib.Path({str(root)!r})
inside_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
outside_fd = os.open({str(outside_dir)!r}, os.O_RDONLY | os.O_DIRECTORY)
try:
    install_runtime_guards(root)
    with tempfile.TemporaryDirectory(dir=root) as nested:
        pathlib.Path(nested, 'temporary.txt').write_text('temporary', encoding='utf-8')
    os.unlink('inside.txt', dir_fd=inside_fd)
    try:
        os.unlink('outside.txt', dir_fd=outside_fd)
    except SafeE2EError as exc:
        print(exc.reason_code)
    else:
        raise SystemExit(8)
finally:
    os.close(inside_fd)
    os.close(outside_fd)
"""
            result = self._run_probe(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WRITE_OUTSIDE_ROOT", result.stdout)
            self.assertFalse(inside.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")


if __name__ == "__main__":
    unittest.main()
