#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.updater.constants import (  # noqa: E402
    ALLOWED_RELEASE_REPOSITORIES,
    KEY_ID,
    LEGACY_RELEASE_REPOSITORY,
)
from src.core.updater import network as network_module  # noqa: E402
from src.core.updater import service as service_module  # noqa: E402

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_BUILD = re.compile(r"^[0-9]{8}\.[0-9]+$")


def release_urls(repository: str) -> tuple[str, str]:
    if repository not in ALLOWED_RELEASE_REPOSITORIES:
        raise ValueError("unsupported repository")
    base = f"https://github.com/{repository}/releases/latest/download/version.json"
    return base, base + ".sig"


def client_prefixes(policy: str) -> tuple[str, ...]:
    if policy == "legacy-0.2.13":
        return (f"/{LEGACY_RELEASE_REPOSITORY}/releases/",)
    if policy == "current":
        return tuple(
            f"/{repository}/releases/"
            for repository in sorted(ALLOWED_RELEASE_REPOSITORIES)
        )
    raise ValueError("unsupported client policy")


def load_public_key(path: Path) -> str:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > 16384
    ):
        raise ValueError("public key path is unsafe")
    value = path.read_text(encoding="ascii")
    if "BEGIN PUBLIC KEY" not in value:
        raise ValueError("public key is invalid")
    return value


def run_probe(
    *,
    repository: str,
    client_policy: str,
    current_version: str,
    current_build: str,
    expected_ui_state: str,
    public_key: str,
) -> dict[str, object]:
    if not _VERSION.fullmatch(current_version) or not _BUILD.fullmatch(current_build):
        raise ValueError("invalid client identity")
    manifest_url, signature_url = release_urls(repository)
    original = {
        "prefixes": network_module.ALLOWED_GITHUB_RELEASE_PREFIXES,
        "manifest_url": service_module.MANIFEST_URL,
        "signature_url": service_module.SIGNATURE_URL,
        "version": service_module.__version__,
        "build": service_module.__build__,
        "runtime_channel": service_module.runtime_channel,
    }
    try:
        network_module.ALLOWED_GITHUB_RELEASE_PREFIXES = client_prefixes(client_policy)
        service_module.MANIFEST_URL = manifest_url
        service_module.SIGNATURE_URL = signature_url
        service_module.__version__ = current_version
        service_module.__build__ = current_build
        service_module.runtime_channel = lambda: "production"
        with tempfile.TemporaryDirectory(prefix="lulu-update-live-probe-") as temp:
            snapshot = service_module.UpdateService(
                root=Path(temp) / "updates",
                public_keys={KEY_ID: public_key},
            ).check(manual=True)
    finally:
        network_module.ALLOWED_GITHUB_RELEASE_PREFIXES = original["prefixes"]
        service_module.MANIFEST_URL = original["manifest_url"]
        service_module.SIGNATURE_URL = original["signature_url"]
        service_module.__version__ = original["version"]
        service_module.__build__ = original["build"]
        service_module.runtime_channel = original["runtime_channel"]
    result = {
        "schema": 1,
        "repository": repository,
        "client_policy": client_policy,
        "current_version": current_version,
        "current_build": current_build,
        "ui_state": snapshot.ui_state,
        "error_code": snapshot.error_code,
        "latest_version": snapshot.latest_version,
        "latest_build": snapshot.latest_build,
        "verified_manifest": snapshot.verified_manifest,
        "pass": snapshot.ui_state == expected_ui_state,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository", choices=sorted(ALLOWED_RELEASE_REPOSITORIES), required=True
    )
    parser.add_argument(
        "--client-policy", choices=("legacy-0.2.13", "current"), required=True
    )
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--current-build", required=True)
    parser.add_argument(
        "--expect", choices=("update_available", "up_to_date"), required=True
    )
    parser.add_argument("--public-key", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_probe(
            repository=args.repository,
            client_policy=args.client_policy,
            current_version=args.current_version,
            current_build=args.current_build,
            expected_ui_state=args.expect,
            public_key=load_public_key(Path(args.public_key).expanduser()),
        )
    except Exception as exc:
        result = {
            "schema": 1,
            "pass": False,
            "error": type(exc).__name__,
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result.get("pass") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
