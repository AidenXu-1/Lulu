"""Keep default model/config writes out of user data for the entire test run.

Individual fixtures still isolate their own state. This outer boundary also
covers a worker that starts after a fixture's mock has been unwound.
"""
from pathlib import Path
import os
import tempfile
from unittest.mock import patch
import unittest


class RealKeychainAccessForbidden(BaseException):
    """Fail the test instead of entering product credential fallbacks or OS UI."""


def deny_real_keychain(*_args, **_kwargs):
    raise RealKeychainAccessForbidden('Capability tests require an injected credential backend')


if __name__ == "__main__":
    os.environ["LULU_TEST_SECRET_STORE"] = "memory"
    with tempfile.TemporaryDirectory(prefix="lulu-capability-data-") as directory, \
            patch.object(Path, "home", return_value=Path(directory).resolve()):
        # Import after home isolation: module-level default paths are otherwise real.
        from src import config
        from src.core import models_manager, library
        from src.core.license import store as license_store
        import keyring
        root = Path(directory).resolve()
        with patch.object(license_store._MacOSExplicitKeyring, "__init__", side_effect=deny_real_keychain), \
                patch.object(keyring, "get_password", side_effect=deny_real_keychain), \
                patch.object(keyring, "set_password", side_effect=deny_real_keychain), \
                patch.object(keyring, "delete_password", side_effect=deny_real_keychain), \
                patch.object(models_manager, "models_root", return_value=root / "models"), \
                patch.object(models_manager, "app_data_dir", return_value=root / "data"), \
                patch.object(config, "_CONFIG_DIR", root / "config"), \
                patch.object(config, "_CONFIG_PATH", root / "config/config.json"), \
                patch.object(library, "LIBRARY_DIR", root / "library"), \
                patch.object(library, "FOLDERS_FILE", root / "library/folders.json"):
            unittest.main(module=None, verbosity=2)
