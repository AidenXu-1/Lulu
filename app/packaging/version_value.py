#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
values = runpy.run_path(str(root / "src" / "version.py"))
field = sys.argv[1] if len(sys.argv) > 1 else "version"
mapping = {"version": "__version__", "build": "__build__", "channel": "__channel__"}
if field == "profile-channel":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    print(values["channel_for_profile"](sys.argv[2]))
    raise SystemExit(0)
if field not in mapping:
    raise SystemExit(2)
print(values[mapping[field]])
