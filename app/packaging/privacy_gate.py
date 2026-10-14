#!/usr/bin/env python3
"""Release artifact privacy cleanup and final-bundle gate.

Only stable finding categories and bundle-relative paths leave this module.
Matched private values and builder paths are never copied into receipts.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat


_CACHE_DIR_NAMES = {"__pycache__"}
_CACHE_SUFFIXES = {".pyc", ".pyo"}
_SENSITIVE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
}
_SENSITIVE_BASENAMES = {
    ".env",
    "cookies",
    "cookies.txt",
    "cookie.txt",
}
_PRIVATE_KEY_RE = re.compile(
    br"-----BEGIN (?P<label>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY)-----\r?\n"
    br"(?P<body>(?:[A-Za-z0-9+/=]{16,}\r?\n){2,})"
    br"-----END (?P=label)-----"
)
_SCAN_BLOCK = 1024 * 1024
_SCAN_OVERLAP = 512 * 1024
_NON_RUNTIME_MARKERS = (
    "sklearn/datasets/tests/",
    "torch/fx/passes/tests/",
)
_FORBIDDEN_PROJECT_PREFIXES = (
    ".git/",
    "app/tests/",
    "docs/collaboration/",
    "scratch/",
    "tests/",
)
_ALLOWED_FIRST_PARTY_SOURCES = {
    "src/core/synthesis/indextts2_worker.py",
    "src/core/synthesis/voxcpm2_worker.py",
    "src/core/synthesis/qwen3_worker.py",
}


def _normal(value: object) -> str:
    return str(value).replace(os.sep, "/")


def is_cache_artifact(value: object) -> bool:
    normalized = _normal(value)
    parts = [part.lower() for part in normalized.split("/") if part]
    return bool(
        _CACHE_DIR_NAMES.intersection(parts)
        or any(normalized.lower().endswith(suffix) for suffix in _CACHE_SUFFIXES)
    )


def drop_cache_toc_entries(items):
    """Drop PyInstaller TOC members whose source or destination is cache output."""

    return [
        item
        for item in items
        if not any(is_cache_artifact(part) for part in item)
    ]


def is_non_runtime_artifact(value: object) -> bool:
    normalized = _normal(value).lstrip("/").lower()
    # PEP 610 local-wheel provenance contains builder paths and is not needed
    # to import the package or inspect its version/license metadata.
    local_provenance = bool(re.search(r"(?:^|/)[^/]+\.dist-info/direct_url\.json$", normalized))
    return local_provenance or any(marker in normalized for marker in _NON_RUNTIME_MARKERS)


def drop_non_runtime_toc_entries(items):
    """Drop third-party test data and installer provenance, keeping runtime metadata."""

    return [
        item
        for item in items
        if not any(is_non_runtime_artifact(part) for part in item)
    ]


def _minimal_bundle_finding(rel: str) -> dict[str, str] | None:
    normalized = _normal(rel).lstrip("/")
    lowered = normalized.lower()
    if any(lowered.startswith(prefix) for prefix in _FORBIDDEN_PROJECT_PREFIXES):
        return {"kind": "non_runtime_artifact", "value": normalized}
    if is_non_runtime_artifact(normalized):
        return {"kind": "non_runtime_artifact", "value": normalized}
    if (
        lowered.startswith("src/")
        and lowered.endswith(".py")
        and normalized not in _ALLOWED_FIRST_PARTY_SOURCES
    ):
        return {"kind": "unexpected_product_source", "value": normalized}
    return None


def _sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return bool(
        lowered in _SENSITIVE_BASENAMES
        or lowered.startswith(".env.")
        or Path(lowered).suffix in _SENSITIVE_SUFFIXES
    )


def _content_findings(path: Path, rel: str, markers: tuple[bytes, ...]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    pending = {marker for marker in markers if marker}
    private_key_found = False
    overlap = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(_SCAN_BLOCK)
            if not block:
                break
            data = overlap + block
            if pending:
                matched = {marker for marker in pending if marker in data}
                if matched:
                    findings.append({"kind": "builder_path", "value": rel})
                    pending.clear()
            if not private_key_found and _PRIVATE_KEY_RE.search(data):
                findings.append({"kind": "private_key_payload", "value": rel})
                private_key_found = True
            overlap = data[-_SCAN_OVERLAP:]
    return findings


def scan_bundle_privacy(root: Path, *, forbidden_markers=()) -> list[dict[str, str]]:
    """Return privacy findings without exposing the matched private values."""

    root = Path(root)
    root_st = root.lstat()
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        raise SystemExit("隐私扫描根必须是非符号链接目录")

    markers = tuple(
        marker if isinstance(marker, bytes) else str(marker).encode("utf-8")
        for marker in forbidden_markers
        if marker
    )
    findings: list[dict[str, str]] = []

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in sorted(dirs):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            if is_cache_artifact(rel):
                findings.append({"kind": "cache_artifact", "value": rel})
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                if any(marker in target for marker in markers):
                    findings.append({"kind": "builder_path", "value": rel})
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(files):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            minimal_finding = _minimal_bundle_finding(rel)
            if minimal_finding is not None:
                findings.append(minimal_finding)
            if is_cache_artifact(rel):
                findings.append({"kind": "cache_artifact", "value": rel})
            if _sensitive_name(name):
                findings.append({"kind": "sensitive_file", "value": rel})
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path).encode("utf-8", "surrogateescape")
                if any(marker in target for marker in markers):
                    findings.append({"kind": "builder_path", "value": rel})
                continue
            if stat.S_ISREG(st.st_mode):
                findings.extend(_content_findings(path, rel, markers))

    return [
        {"kind": kind, "value": value}
        for kind, value in sorted(
            {
                (item["kind"], item["value"])
                for item in findings
            },
            key=lambda item: (item[1], item[0]),
        )
    ]
