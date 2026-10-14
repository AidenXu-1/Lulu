#!/usr/bin/env python3
"""Remove one exact Safe E2E root from its opaque authority receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.trusted_test_safe_e2e import (  # noqa: E402
    cleanup_isolated_root,
    load_safe_authority,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authority", required=True)
    parser.add_argument("--candidate-app", required=True)
    args = parser.parse_args(argv)
    executable = (
        Path(args.candidate_app)
        / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
    )
    receipt = load_safe_authority(
        Path(args.authority),
        executable_path=executable,
    )
    result = cleanup_isolated_root(receipt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
