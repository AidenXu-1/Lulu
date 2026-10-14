"""已停用的历史命令行入口。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    sys.stderr.write("LEGACY_CLI_DISABLED\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
