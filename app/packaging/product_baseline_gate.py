#!/usr/bin/env python3
"""Reject packages that are not built from Lulu's accepted complete product line."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile


# Accepted public 4.2.1 product baseline, available in a normal clone.
COMPLETE_PRODUCT_BASELINE = "6a52b3a6bc8521012d3c01d2e49e05c3f99394f9"
_SOURCE_REPOSITORY = runpy.run_path(str(Path(__file__).with_name("source_repository.py")))
_OBJECT_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _resolve_commit(repo: Path, revision: str, label: str) -> str:
    result = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    value = result.stdout.strip()
    if result.returncode != 0 or not _OBJECT_RE.fullmatch(value):
        raise SystemExit(f"无法核验{label} Git 提交：{revision}")
    return value


def verify_product_baseline(
    *,
    repo: Path,
    candidate: str = "HEAD",
    baseline: str | None = None,
) -> dict:
    repo = Path(repo).resolve()
    _SOURCE_REPOSITORY["prepare_repository"](repo)
    baseline = baseline or _SOURCE_REPOSITORY["default_baseline"](repo, COMPLETE_PRODUCT_BASELINE)
    candidate_commit = _resolve_commit(repo, candidate, "候选")
    baseline_commit = _resolve_commit(repo, baseline, "完整产品基线")
    ancestry = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        baseline_commit,
        candidate_commit,
    )
    if ancestry.returncode == 1:
        raise SystemExit(
            "候选源码未继承已验收的完整产品基线，拒绝打包；"
            "请先把修复移植到最新版完整产品主线。"
        )
    if ancestry.returncode != 0:
        raise SystemExit("无法核验候选与完整产品基线的祖先关系")
    tree = _git(repo, "rev-parse", "--verify", f"{candidate_commit}^{{tree}}")
    candidate_tree = tree.stdout.strip()
    if tree.returncode != 0 or not _OBJECT_RE.fullmatch(candidate_tree):
        raise SystemExit("无法核验候选 Git tree")
    return {
        "schema_version": 1,
        "baseline_commit": baseline_commit,
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "checks": {
            "candidate_descends_from_baseline": True,
        },
    }


def write_product_baseline_receipt(path: Path, payload: dict) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--baseline")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = verify_product_baseline(
        repo=args.repo,
        candidate=args.candidate,
        baseline=args.baseline,
    )
    write_product_baseline_receipt(args.output, receipt)
    print(
        "完整产品基线校验通过："
        f"{receipt['baseline_commit'][:12]} -> {receipt['candidate_commit'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
