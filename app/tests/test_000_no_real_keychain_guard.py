"""Default-discovery guard: tests must never reach a real system keychain.

The filename is deliberate. ``unittest discover`` sorts ``test_*.py`` names,
so this module installs the process-wide test guard before other test modules
are imported. Production startup never imports this module.
"""

import shlex
import subprocess
import unittest
from pathlib import Path

from src.core import secrets as generic_secrets
from src.core.license import store as license_store


class RealKeychainAccessForbidden(BaseException):
    """Escape product ``except Exception`` fallbacks and fail the test immediately."""


class _ForbiddenMacOSKeyring:
    def __init__(self, *_args, **_kwargs):
        raise RealKeychainAccessForbidden(
            "自动化测试禁止实例化真实 _MacOSExplicitKeyring；请注入 Memory/Fake/Mock 后端"
        )


class _ForbiddenGenericKeyring:
    @staticmethod
    def _fail():
        raise RealKeychainAccessForbidden(
            "自动化测试禁止访问真实 keyring 后端；请将 src.core.secrets._backend 注入为 None/Fake/Mock"
        )

    def get_password(self, *_args, **_kwargs):
        self._fail()

    def set_password(self, *_args, **_kwargs):
        self._fail()

    def delete_password(self, *_args, **_kwargs):
        self._fail()


_FORBIDDEN_GENERIC_BACKEND = _ForbiddenGenericKeyring()
_ORIGINAL_POPEN = subprocess.Popen


def _guarded_popen(args, *popen_args, **popen_kwargs):
    command = shlex.split(args) if isinstance(args, str) else list(args)
    executable = command[0] if command else ""
    if Path(str(executable)).name == "security":
        raise RealKeychainAccessForbidden(
            "自动化测试禁止执行 macOS security 命令；请使用纯模拟安全存储后端"
        )
    return _ORIGINAL_POPEN(args, *popen_args, **popen_kwargs)


license_store._MacOSExplicitKeyring = _ForbiddenMacOSKeyring
generic_secrets._backend = lambda: _FORBIDDEN_GENERIC_BACKEND
subprocess.Popen = _guarded_popen


class DefaultKeychainGuardTests(unittest.TestCase):
    def test_license_store_guard_fails_before_security_framework(self):
        with self.assertRaises(RealKeychainAccessForbidden):
            license_store._MacOSExplicitKeyring("/missing/test.keychain-db")

    def test_generic_secret_guard_is_not_swallowed_by_memory_fallback(self):
        with self.assertRaises(RealKeychainAccessForbidden):
            generic_secrets.get_key("unit-probe")

    def test_security_command_guard_fails_before_process_launch(self):
        with self.assertRaises(RealKeychainAccessForbidden):
            subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "Lulu.UnitProbe"],
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
