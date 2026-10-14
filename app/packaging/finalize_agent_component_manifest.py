#!/usr/bin/env python3
"""Replace the build-time placeholder with the exact generation-9 closed manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from src.core.agent_entry.component_manifest import build_component_manifest
from src.core.agent_entry.delivery_identity import MANIFEST_NAME, prepare_link_policy, verify_delivery_marker


FIRST_BATCH_CAPABILITIES = (
    "AUTH-01",
    "JOB-01",
    "JOB-02",
    "JOB-03",
    "JOB-05",
    "JOB-06",
    "SYS-01",
    "SYS-02",
    "SYS-04",
    "TR-01",
    "TR-02",
    "TR-03",
)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _expected_skill_identity(args: argparse.Namespace) -> dict[str, object]:
    return {
        "product_version": args.version,
        "product_build": args.build,
        "source_revision": args.source_revision,
        "installation_profile": "circle_internal",
    }


def _prepare_skill(args: argparse.Namespace) -> None:
    app = Path(args.app)
    skill_manifest_path = app / "Contents/Resources/skills/lulu-agent/manifest.json"
    skill_manifest = json.loads(skill_manifest_path.read_text(encoding="utf-8"))
    skill_manifest["candidate_generation"] = 9
    skill_manifest["product_identity"] = _expected_skill_identity(args)
    _atomic_json(skill_manifest_path, skill_manifest)
    verify_delivery_marker(app)
    prepare_link_policy(app)
    from src.core.agent_entry.standalone_source import prepare_install_descriptor
    prepare_install_descriptor(app, product_version=args.version, product_build=args.build,
        source_revision=args.source_revision, capability_closed_set=FIRST_BATCH_CAPABILITIES)
    # Both delivery formats use these exact bytes. Normalize before final signing,
    # never re-sign a separate ZIP copy into a different App identity.
    for root, directories, files in os.walk(app, followlinks=False):
        for name in directories:
            path = Path(root)/name
            if not path.is_symlink():
                path.chmod(0o755)
        for name in files:
            path = Path(root)/name
            if path.is_symlink():
                continue
            executable = path.stat().st_mode & 0o111 and path.relative_to(app).as_posix().startswith(
                ('Contents/MacOS/', 'Contents/Helpers/', 'Contents/Frameworks/'))
            path.chmod(0o755 if executable else 0o644)


def _finalize_manifest(args: argparse.Namespace) -> None:
    app = Path(args.app)
    manifest_path = app.parent / MANIFEST_NAME
    skill_manifest_path = app / "Contents/Resources/skills/lulu-agent/manifest.json"
    skill_manifest = json.loads(skill_manifest_path.read_text(encoding="utf-8"))
    if (
        skill_manifest.get("candidate_generation") != 9
        or skill_manifest.get("product_identity") != _expected_skill_identity(args)
    ):
        raise SystemExit("Skill 身份必须在签名与候选 tree 计算前定型")
    manifest = build_component_manifest(
        app_bundle=app,
        manifest_path=manifest_path,
        product_version=args.version,
        product_build=args.build,
        source_revision=args.source_revision,
        candidate_generation=9,
        cli_protocol_min="1.0.0",
        cli_protocol_max="2.0.0",
        catalog_relative_path="Contents/Resources/src/agent_cli/capability_catalog.json",
        capability_closed_set=FIRST_BATCH_CAPABILITIES,
    )
    _atomic_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare-skill", "finalize-manifest"))
    parser.add_argument("--app", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    if args.phase == "prepare-skill":
        _prepare_skill(args)
    else:
        _finalize_manifest(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
