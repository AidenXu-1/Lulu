#!/usr/bin/env python3
"""Create one isolated run root for the dedicated Safe E2E test package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.trusted_test_safe_e2e import (  # noqa: E402
    SafeE2EError,
    create_private_run_root,
    prepare_isolated_authority,
)


_BASE = Path("/private/tmp/lulu-trusted-test-safe-e2e")


def _ensure_base() -> Path:
    try:
        _BASE.mkdir(mode=0o700)
        os.chmod(_BASE, 0o700, follow_symlinks=False)
    except FileExistsError:
        pass
    metadata = _BASE.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SafeE2EError("RUN_BASE_INVALID")
    return _BASE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-app", required=True)
    parser.add_argument("--model-seed", required=True)
    parser.add_argument("--source-file", required=True)
    args = parser.parse_args(argv)
    root = create_private_run_root(_ensure_base())
    receipt = prepare_isolated_authority(
        run_root=root,
        candidate_app=Path(args.candidate_app),
        model_seed=Path(args.model_seed),
        source_file=Path(args.source_file),
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
