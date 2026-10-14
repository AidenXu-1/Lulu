"""Frozen console entry for the Trusted Test isolated local E2E harness."""

from __future__ import annotations

import multiprocessing


def main() -> int:
    from src.trusted_test_safe_e2e import main as safe_main

    return int(safe_main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
