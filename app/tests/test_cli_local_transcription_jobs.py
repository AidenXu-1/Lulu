from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.agent_cli.main import main
from src.core.auth_guard import UnifiedLicenseAuthGuard
from src.core.job_ledger.contracts import JobState
from src.core.job_ledger.service import JobControlService
from src.core.job_ledger.store import JobLedgerStore
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments


class _FreshCachedLicense:
    def __init__(self) -> None:
        self.calls = 0

    def cached_status_projection(self):
        self.calls += 1
        return SimpleNamespace(allowed=True, code="cached_allowed")


if __name__ == "__main__":
    unittest.main()
