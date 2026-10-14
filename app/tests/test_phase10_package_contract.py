"""Phase 10 one-App dual-entry package and candidate finalization contract."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import plistlib
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src.core.agent_entry.canonical_app_tree import canonical_app_tree_identity


_VERSION = "0.3.2"
_BUILD = "20260823.4"
_FIXED_BUILD_PYTHON = Path(
    "/Users/aiden/AidenWorkflow/2-AI/01产品开发/01APP/"
    "Lulu/.venv/bin/python"
)
_PROFILE_GATE_R1_REAL_FAILURE = {
    "false_checks": [
        "bundle_executable_contract_valid",
        "final_forbidden_zero",
        "inventory_difference_zero",
        "inventory_digest_matches",
    ],
    "final_findings": [
        {
            "kind": "unknown_executable_member",
            "value": "Contents/MacOS/lulu-agent-cli",
        }
    ],
    "modules_missing": ["run_agent_cli"],
    "archive_entries": ["run_external"],
}
_PROFILE_GATE_FIXTURE_FILES = (
    "packaging/Lulu.spec",
    "packaging/macos_compatibility.py",
    "packaging/materialize_app_symlinks.py",
    "packaging/privacy_gate.py",
    "packaging/profile_gate.py",
    "run_external.py",
    "src/core/license/external_service.py",
    "src/core/agent_entry/canonical_app_tree.py",
    "src/core/pipeline.py",
    "src/core/synthesis/indextts2_worker.py",
    "src/core/synthesis/voxcpm2_worker.py",
    "src/main.py",
    "src/release_features.py",
    "src/ui/main_window.py",
    "THIRD_PARTY_NOTICES_DOUYIN_COLLECT.md",
    "THIRD_PARTY_NOTICES_VOXCPM2.md",
)


class _FakeAdHocCodesign:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> SimpleNamespace:
        self.calls.append(list(argv))
        if argv[1:4] == ["--verify", "--deep", "--strict"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["--display", "--verbose=4"]:
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(
                    "Executable=" + argv[-1] + "\n"
                    "Identifier=com.lulu.transcribe\n"
                    "Signature=adhoc\n"
                    "TeamIdentifier=not set\n"
                ),
            )
        raise AssertionError(f"unexpected codesign invocation: {argv!r}")


def _write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.write_bytes(content)
    os.chmod(path, mode)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process_env.update(env or {})
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=process_env,
    )


def _profile_gate_fixture(root: Path) -> tuple[Path, Path]:
    source_app = Path(__file__).resolve().parents[1]
    repo = root / "fixture repo"
    app = repo / "app"
    for relative in _PROFILE_GATE_FIXTURE_FILES:
        source = source_app / relative
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for argv in (
        ["/usr/bin/git", "init", "--quiet"],
        ["/usr/bin/git", "add", "--", "app"],
        [
            "/usr/bin/git",
            "-c",
            "user.name=Phase10 Test",
            "-c",
            "user.email=phase10@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    ):
        result = _run(argv, cwd=repo)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
    _write(app / "license_runtime_config.json", b"{}\n")
    return repo, app


def _fake_build_venv(root: Path, *, python: Path | None = None) -> Path:
    venv = root / "existing python311 venv"
    (venv / "bin").mkdir(parents=True, mode=0o700)
    os.chmod(venv, 0o700)
    (venv / "bin" / "python").symlink_to(
        python or Path(sys.executable).resolve()
    )
    return venv


def _authority_loader_fixture(
    root: Path,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    source_app = Path(__file__).resolve().parents[1]
    fixture_app = root / "workspace" / "app"
    packaging = fixture_app / "packaging"
    packaging.mkdir(parents=True, mode=0o700)
    support = root / "support"
    support.mkdir(mode=0o700)

    codesign = support / "codesign"
    _write(
        codesign,
        b"""#!/bin/sh
set -eu
if [ "$1" = "--verify" ]; then exit 0; fi
if [ "$1" = "-d" ]; then printf 'Signature=adhoc\\n' >&2; exit 0; fi
exit 97
""",
        0o700,
    )
    wrapper = packaging / "build_trusted_test.sh"
    wrapper_source = (
        source_app / "packaging" / "build_trusted_test.sh"
    ).read_text(encoding="utf-8")
    _write(
        wrapper,
        wrapper_source.replace("/usr/bin/codesign", str(codesign)).encode(
            "utf-8"
        ),
        0o700,
    )
    _write(
        packaging / "build_app.sh",
        b"""#!/bin/bash
set -euo pipefail
mkdir -p "$LULU_DIST_ROOT/Lulu-Trusted-Test.app"
""",
        0o700,
    )

    authority_source = packaging / "phase10_post_build_authority.py"
    recorder = root / "authority-loader-record.json"
    authority_stub = f'''from __future__ import annotations

import json
from pathlib import Path

_EVENTS = []
_RECORDER = Path({str(recorder)!r})


class _Build:
    pass


class _Carrier:
    pass


class _SecurityApp:
    pass


class _TestApp:
    pass


class _Artifact:
    pass


class _SecurityArtifact:
    pass


class _TestArtifact:
    pass


class _Authority:
    pass


def _take(name, value, expected):
    if type(value) is not expected:
        raise TypeError(f"{{name}} receipt type mismatch")
    _EVENTS.append(name)


def issue_build_completed_receipt():
    _EVENTS.append("issue_build_completed_receipt")
    return _Build()


def produce_carrier(build):
    _take("produce_carrier", build, _Build)
    return _Carrier()


def review_carrier_security(carrier):
    _take("review_carrier_security", carrier, _Carrier)
    return _SecurityApp()


def review_carrier_test(carrier):
    _take("review_carrier_test", carrier, _Carrier)
    return _TestApp()


def finalize_authority_artifact(carrier, security_app, test_app):
    _take("finalize_authority_artifact", carrier, _Carrier)
    if type(security_app) is not _SecurityApp or type(test_app) is not _TestApp:
        raise TypeError("application review receipt type mismatch")
    return _Artifact()


def review_authority_artifact_security(artifact):
    _take("review_authority_artifact_security", artifact, _Artifact)
    return _SecurityArtifact()


def review_authority_artifact_test(artifact):
    _take("review_authority_artifact_test", artifact, _Artifact)
    return _TestArtifact()


def issue_post_build_authority(artifact, security_artifact, test_artifact):
    _take("issue_post_build_authority", artifact, _Artifact)
    if (
        type(security_artifact) is not _SecurityArtifact
        or type(test_artifact) is not _TestArtifact
    ):
        raise TypeError("artifact review receipt type mismatch")
    return _Authority()


def post_build_public_fingerprint(authority):
    _take("post_build_public_fingerprint", authority, _Authority)
    payload = {{
        "events": _EVENTS,
        "module_file": __file__,
        "module_origin": __spec__.origin,
        "loader_type": (
            type(__loader__).__module__ + "." + type(__loader__).__qualname__
        ),
    }}
    _RECORDER.write_text(json.dumps(payload, sort_keys=True) + "\\n", encoding="utf-8")
    return {{"status": "authority-issued"}}
'''
    _write(authority_source, authority_stub.encode("utf-8"))

    venv = root / "fixed python311 venv"
    (venv / "bin").mkdir(parents=True, mode=0o700)
    python_dispatcher = f'''#!{_FIXED_BUILD_PYTHON}
import os
from pathlib import Path
import sys

args = sys.argv[1:]
if args[:1] == ["packaging/version_value.py"]:
    values = {{"version": "0.3.2", "build": "{_BUILD}", "profile-channel": "trusted-test"}}
    print(values[args[1]])
    raise SystemExit(0)
if args[:1] == ["packaging/phase10_finalize_candidate.py"]:
    output = Path(args[args.index("--output") + 1])
    output.write_text("{{}}\\n", encoding="utf-8")
    raise SystemExit(0)
if args == ["-E", "-s", "-S", "-"]:
    os.execv(sys.executable, [sys.executable, *args])
raise SystemExit(98)
'''
    _write(venv / "bin" / "python", python_dispatcher.encode("utf-8"), 0o700)
    authority_root = fixture_app / "release" / "post-build-authority"
    return (
        fixture_app,
        wrapper,
        venv,
        recorder,
        authority_source,
        authority_root,
    )


def _profile_create_command(
    app: Path,
    venv: Path,
    *,
    executable: Path | None = None,
) -> list[str]:
    return [
        str(executable or venv / "bin" / "python"),
        str(app / "packaging" / "profile_gate.py"),
        "create",
        "--profile",
        "external_candidate",
        "--version",
        _VERSION,
        "--build",
        _BUILD,
        "--build-venv-root",
        str(venv),
        "--output",
        str(app / "build_profile.json"),
        "--product-source-output",
        str(app / "product_source.json"),
    ]


def _profile_gate_failure_projection(receipt: dict) -> dict:
    return {
        "false_checks": sorted(
            name for name, passed in receipt["checks"].items() if not passed
        ),
        "final_findings": receipt["final_findings"],
        "modules_missing": receipt["inventory"]["differences"]["modules_missing"],
        "archive_entries": receipt["inventory"]["archive"]["app_entry_scripts"],
    }


def _dual_entry_profile_bundle(root: Path, namespace: dict) -> tuple[Path, Path, object]:
    app_root = Path(__file__).resolve().parents[1]
    app = root / "Lulu.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    signature = contents / "_CodeSignature"
    macos.mkdir(parents=True, mode=0o700)
    resources.mkdir(mode=0o700)
    signature.mkdir(mode=0o700)
    for path in (app, contents, macos, resources, signature):
        os.chmod(path, 0o700)
    _write(macos / "Lulu", b"\xcf\xfa\xed\xfefake gui archive", 0o700)
    _write(
        macos / "lulu-agent-cli",
        b"\xcf\xfa\xed\xfefake cli archive",
        0o700,
    )
    _write(signature / "CodeResources", b"fake sealed resources")
    _write(
        contents / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lulu.transcribe",
                "CFBundleName": "Lulu",
                "CFBundleExecutable": "Lulu",
                "CFBundlePackageType": "APPL",
            }
        ),
    )
    product_source = namespace["product_source_manifest"](
        "external_candidate",
        source_commit="a" * 40,
        source_tree="b" * 40,
        source_loader=lambda repo_path: (
            app_root / repo_path.removeprefix("app/")
        ).read_bytes(),
    )
    product_source_digest = namespace["write_product_source"](
        resources / "product_source.json",
        product_source,
    )
    profile = namespace["write_profile"](
        resources / "build_profile.json",
        "external_candidate",
        version=_VERSION,
        build=_BUILD,
        source_commit="a" * 40,
        source_tree="b" * 40,
        product_source_sha256=product_source_digest,
    )
    profile_sha = hashlib.sha256(
        (resources / "build_profile.json").read_bytes()
    ).hexdigest()
    _write(
        resources / "AUTHORSHIP.json",
        (
            json.dumps(
                {
                    "build_profile": "external_candidate",
                    "build_profile_sha256": profile_sha,
                    "source_commit": profile["source_commit"],
                    "source_tree": profile["source_tree"],
                    "product_source_sha256": product_source_digest,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    _write(
        resources / "license_runtime_config.json",
        b'{"schema_version":1,"environment":"production","endpoint":"https://license.lulu.example/api","build_profile":"external_candidate"}\n',
    )
    _write(resources / "agent_product_manifest.json", b"placeholder\n")
    for bundle_path, record in product_source["resources"].items():
        destination = resources / bundle_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write(
            destination,
            (app_root / record["path"].removeprefix("app/")).read_bytes(),
        )

    shared_modules = set(product_source["required_modules"])
    analysis_modules = sorted(shared_modules | {"run_external", "run_agent_cli"})
    resource_names = namespace["_walk_inventory"](resources)[0]
    analysis = root / "profile-analysis.json"
    namespace["write_analysis_receipt"](
        analysis,
        "external_candidate",
        [(name, str(app_root / "src" / "main.py")) for name in analysis_modules],
        resource_names,
        profile_sha,
    )
    product_hashes = {
        name: record["code_sha256"]
        for name, record in product_source["modules"].items()
    }
    product_hashes["run_agent_cli"] = namespace["_code_sha256"](
        (app_root / "run_agent_cli.py").read_bytes(),
        "app/run_agent_cli.py",
    )

    def archive_reader(executable: Path, _profile: str):
        entry = "run_agent_cli" if executable.name == "lulu-agent-cli" else "run_external"
        modules = sorted(shared_modules | {entry})
        return modules, [], {
            "format": "pyinstaller_carchive",
            "carchive_count": 15,
            "pyz_archive_count": 1,
            "pyz_count": len(shared_modules),
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "app_entry_scripts": [entry],
            "product_entry_scripts": [entry],
            "runtime_hook_scripts": ["gui_host_runtime_hook"],
            "product_code_sha256": {
                name: digest
                for name, digest in product_hashes.items()
                if name in shared_modules or name == entry
            },
        }

    return app, analysis, archive_reader


def _build_script_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    repo, app = _profile_gate_fixture(root)
    source_app = Path(__file__).resolve().parents[1]
    shutil.copy2(
        source_app / "packaging" / "build_app.sh",
        app / "packaging" / "build_app.sh",
    )
    (app / "native").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        source_app / "native" / "lulu_agent_xpc_bridge.c",
        app / "native" / "lulu_agent_xpc_bridge.c",
    )
    shutil.copy2(
        source_app / "native" / "lulu_agent_xpc_bridge.h",
        app / "native" / "lulu_agent_xpc_bridge.h",
    )
    shutil.copy2(
        source_app / "packaging" / "com.lulu.transcribe.agent-core.plist",
        app / "packaging" / "com.lulu.transcribe.agent-core.plist",
    )
    stubs = {
        "packaging/version_value.py": b"""\
import sys
values = {"version": "0.3.2", "build": "20260823.1", "channel": "candidate"}
print(values[sys.argv[1]])
""",
        "packaging/phase10_prepare_build_roots.py": b"""\
import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--app-root")
parser.add_argument("--build-root", required=True)
parser.add_argument("--dist-root", required=True)
parser.add_argument("--identity")
parser.add_argument("--version")
parser.add_argument("--build")
args = parser.parse_args()
Path(args.build_root).mkdir(parents=True, exist_ok=True)
Path(args.dist_root).mkdir(parents=True, exist_ok=True)
""",
        "packaging/product_baseline_gate.py": b"""\
import os
from pathlib import Path
state = "symlink" if Path(".venv").is_symlink() else ("other" if os.path.lexists(".venv") else "absent")
with Path(os.environ["LULU_TEST_EVENT_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(f"baseline:{state}\\n")
""",
        "packaging/product_capability_gate.py": b"""\
import os
from pathlib import Path
state = "symlink" if Path(".venv").is_symlink() else ("other" if os.path.lexists(".venv") else "absent")
with Path(os.environ["LULU_TEST_EVENT_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(f"capability:{state}\\n")
""",
        "packaging/macos_compatibility.py": b"""\
def evaluate_app(_app):
    return {"ok": True}
""",
        "packaging/make_icon.py": b"",
        "packaging/make_authorship.py": b"",
    }
    for relative, content in stubs.items():
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write(destination, content)
    added = [
        "app/packaging/build_app.sh",
        "app/native/lulu_agent_xpc_bridge.c",
        "app/native/lulu_agent_xpc_bridge.h",
        "app/packaging/com.lulu.transcribe.agent-core.plist",
        *[f"app/{item}" for item in stubs],
    ]
    for argv in (
        ["/usr/bin/git", "add", "--", *added],
        [
            "/usr/bin/git",
            "-c",
            "user.name=Phase10 Test",
            "-c",
            "user.email=phase10@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "build fixture",
        ],
    ):
        result = _run(argv, cwd=repo)
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)

    support = root / "test support"
    pyinstaller = support / "modules" / "PyInstaller"
    pyinstaller.mkdir(parents=True)
    _write(pyinstaller / "__init__.py", b"__version__ = 'test'\n")
    _write(
        pyinstaller / "__main__.py",
        b"""\
import os
from pathlib import Path

link = Path.cwd() / ".venv"
state = "symlink" if link.is_symlink() else ("other" if os.path.lexists(link) else "absent")
with Path(os.environ["LULU_TEST_EVENT_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(f"pyinstaller:{state}\\n")
mode = os.environ["LULU_TEST_PYINSTALLER_MODE"]
if mode == "replace":
    link.unlink()
    link.symlink_to(os.environ["LULU_TEST_REPLACEMENT_ROOT"], target_is_directory=True)
    raise SystemExit(23)
if mode == "fail":
    raise SystemExit(23)
""",
    )
    fake_bin = support / "bin"
    fake_bin.mkdir(parents=True)
    for name in ("sips", "iconutil"):
        _write(fake_bin / name, b"#!/bin/sh\nexit 0\n", 0o700)
    _write(
        fake_bin / "find",
        b"""#!/bin/sh
state=absent
if [ -L .venv ]; then state=symlink; elif [ -e .venv ]; then state=other; fi
printf 'find:%s\\n' "$state" >> "$LULU_TEST_EVENT_LOG"
exit 31
""",
        0o700,
    )
    venv = Path(sys.executable).parent.parent
    return repo, app, venv, support


def _run_fake_build(
    app: Path,
    venv: Path,
    support: Path,
    *,
    event_log: Path,
    pyinstaller_mode: str,
    replacement_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "/bin/bash",
            str(app / "packaging" / "build_app.sh"),
            "--identity",
            "candidate",
            "--venv",
            str(venv),
        ],
        cwd=app,
        env={
            "LULU_BUILD_ROOT": str(app / "build" / "phase10-candidate-20260823.1"),
            "LULU_DIST_ROOT": str(app / "dist" / "phase10-candidate-20260823.1"),
            "LULU_LICENSE_ENV": "production",
            "LULU_LICENSE_ENDPOINT": "https://license.lulu.example/api",
            "LULU_TEST_EVENT_LOG": str(event_log),
            "LULU_TEST_PYINSTALLER_MODE": pyinstaller_mode,
            "LULU_TEST_REPLACEMENT_ROOT": str(replacement_root or venv),
            "PATH": f"{support / 'bin'}:{os.environ['PATH']}",
            "PYTHONPATH": str(support / "modules"),
        },
    )


def _fake_bundle(root: Path) -> Path:
    bundle = root / "候选 空格" / "Lulu-Trusted-Test.app"
    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    frameworks = bundle / "Contents" / "Frameworks"
    macos.mkdir(parents=True, mode=0o700)
    resources.mkdir(mode=0o700)
    frameworks.mkdir(mode=0o700)
    for path in (bundle, bundle / "Contents", macos, resources, frameworks):
        os.chmod(path, 0o700)
    _write(macos / "Lulu-Trusted-Test", b"fake windowed executable", 0o700)
    _write(frameworks / "shared-python-runtime", b"one shared runtime")
    _write(
        bundle / "Contents" / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                "CFBundleExecutable": "Lulu-Trusted-Test",
                "CFBundleShortVersionString": _VERSION,
                "CFBundleVersion": _BUILD,
                "LuluBuildProfile": "trusted_test",
                "LuluBuildIdentity": "trusted-test",
            }
        ),
    )
    _write(
        resources / "build_profile.json",
        (
            json.dumps(
                {
                    "schema": 1,
                    "profile": "trusted_test",
                    "version": _VERSION,
                    "build": _BUILD,
                    "source_commit": "1" * 40,
                    "source_tree": "2" * 40,
                    "product_source_sha256": "3" * 64,
                    "app_name": "Lulu Trusted Test",
                    "bundle_id": "com.lulu.transcribe.trusted-test",
                    "artifact_prefix": "Lulu-Trusted-Test",
                    "upload_target": "none",
                    "entry": "run_trusted_test.py",
                    "package_surface": "gui_only/v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    )
    return bundle


class Phase10PackageContractTests(unittest.TestCase):
    def test_public_profile_verify_delegates_to_full_reader_gate(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(str(app_root / "packaging/profile_gate.py"))
        verify_app = namespace["verify_app"]
        globals_ = verify_app.__globals__
        captured = {}
        globals_["source_identity"] = lambda **_kwargs: ("a" * 40, "b" * 40)
        globals_["product_source_manifest_from_git"] = (
            lambda profile, commit, tree: {
                "profile": profile,
                "source_commit": commit,
                "source_tree": tree,
            }
        )
        globals_["_MACOS_COMPATIBILITY_GATE"] = {
            "evaluate_app": lambda app: {"ok": True, "app": str(app)}
        }

        def fake_reader_gate(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"ok": True}

        globals_["_verify_app_with_reader"] = fake_reader_gate
        result = verify_app(
            Path("/tmp/Lulu.app"),
            "external_candidate",
            Path("/tmp/analysis.json"),
            Path("/tmp/profile.json"),
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["kwargs"]["expected_source_identity"], ("a" * 40, "b" * 40))
        self.assertEqual(captured["kwargs"]["macos_compatibility"]["ok"], True)

    def test_trusted_test_spec_has_gui_only_v1_without_agent_cli_or_hook(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        source = (app_root / "packaging/Lulu.spec").read_text(encoding="utf-8")
        self.assertIn('"package_surface": "gui_only/v1"', source)
        self.assertIn('AGENT_CLI_ENABLED = BUILD_PROFILE == "external_candidate"', source)
        self.assertIn('if BUILD_PROFILE == "external_candidate":\n    RUNTIME_HOOKS', source)
        self.assertNotIn(
            'if BUILD_PROFILE in {"external_candidate", "trusted_test"}:\n    hiddenimports += [\n        "keyring.backends.macOS"',
            source,
        )

    def test_trusted_test_validate_only_reports_absolute_candidate_paths(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        result = _run(
            [
                "/bin/bash",
                str(app_root / "packaging" / "build_trusted_test.sh"),
                "--validate-only",
            ],
            cwd=app_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
        expected_dist = app_root / "dist" / f"phase10-trusted-test-{_BUILD}"
        expected = {
            "build_root": app_root / "build" / f"phase10-trusted-test-{_BUILD}",
            "dist_root": expected_dist,
            "app": expected_dist / "Lulu-Trusted-Test.app",
            "candidate_evidence": expected_dist / "candidate-evidence.json",
        }
        violations: list[str] = []
        for key, path in expected.items():
            value = values.get(key)
            if value is None:
                violations.append(f"missing:{key}")
            elif not Path(value).is_absolute():
                violations.append(f"relative:{key}:{value}")
            elif value != str(path):
                violations.append(f"wrong:{key}:{value}")
        self.assertEqual(violations, [])

    def test_trusted_test_wrapper_derives_candidate_paths_only_from_dist_root(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (
            app_root / "packaging" / "build_trusted_test.sh"
        ).read_text(encoding="utf-8")
        assignments = [
            line.strip()
            for line in script.splitlines()
            if line.startswith("APP_PATH=")
            or line.startswith("CANDIDATE_EVIDENCE_PATH=")
        ]
        expected_assignments = [
            'APP_PATH="$DIST_ROOT/Lulu-Trusted-Test.app"',
            'CANDIDATE_EVIDENCE_PATH="$DIST_ROOT/candidate-evidence.json"',
        ]
        violations: list[str] = []
        if assignments != expected_assignments:
            violations.append(f"path_assignments:{assignments!r}")
        if '--bundle "$APP_PATH"' not in script:
            violations.append("bundle_not_fixed_app_path")
        if '--output "$CANDIDATE_EVIDENCE_PATH"' not in script:
            violations.append("output_not_fixed_evidence_path")
        for forbidden in (
            'APP_PATH="dist/',
            '$(dirname "$APP_PATH")',
            "LULU_APP_PATH",
            "LULU_CANDIDATE_EVIDENCE_PATH",
            "--app-path",
            "--candidate-evidence-path",
            "jq ",
        ):
            if forbidden in script:
                violations.append(f"caller_or_derived_path:{forbidden}")
        self.assertEqual(violations, [])

    def test_trusted_test_wrapper_passes_absolute_paths_to_stub_finalizer(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        source = (app_root / "packaging" / "build_trusted_test.sh").read_text(
            encoding="utf-8"
        )
        self.assertEqual(source.count("/usr/bin/codesign"), 2)
        with tempfile.TemporaryDirectory(prefix="trusted-wrapper-argv-") as directory:
            root = Path(directory).resolve()
            fixture_app = root / "workspace" / "app"
            packaging = fixture_app / "packaging"
            packaging.mkdir(parents=True, mode=0o700)
            support = root / "support"
            support.mkdir(mode=0o700)
            codesign = support / "codesign"
            _write(
                codesign,
                b"""#!/bin/sh
set -eu
if [ "$1" = "--verify" ]; then exit 0; fi
if [ "$1" = "-d" ]; then printf 'Signature=adhoc\\n' >&2; exit 0; fi
exit 97
""",
                0o700,
            )
            wrapper = packaging / "build_trusted_test.sh"
            _write(
                wrapper,
                source.replace("/usr/bin/codesign", str(codesign)).encode("utf-8"),
                0o700,
            )
            _write(
                packaging / "build_app.sh",
                b"""#!/bin/bash
set -euo pipefail
mkdir -p "$LULU_DIST_ROOT/Lulu-Trusted-Test.app"
""",
                0o700,
            )
            venv = root / "fixed python311 venv"
            (venv / "bin").mkdir(parents=True, mode=0o700)
            recorder = root / "finalizer-argv.json"
            python_stub = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

if sys.argv[1:2] == ["packaging/version_value.py"]:
    values = {{"version": "0.3.2", "build": "{_BUILD}", "profile-channel": "trusted-test"}}
    print(values[sys.argv[2]])
    raise SystemExit(0)
if sys.argv[1:2] == ["packaging/phase10_finalize_candidate.py"]:
    Path(os.environ["LULU_TEST_FINALIZER_ARGV"]).write_text(
        json.dumps(sys.argv[1:]) + "\\n", encoding="utf-8"
    )
    raise SystemExit(0)
if sys.argv[1:] == ["-E", "-s", "-S", "-"]:
    sys.stdin.read()
    raise SystemExit(0)
raise SystemExit(98)
"""
            _write(venv / "bin" / "python", python_stub.encode("utf-8"), 0o700)

            result = _run(
                ["/bin/bash", str(wrapper), "--venv", str(venv)],
                cwd=fixture_app,
                env={"LULU_TEST_FINALIZER_ARGV": str(recorder)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(recorder.read_text(encoding="utf-8"))
            bundle = argv[argv.index("--bundle") + 1]
            output = argv[argv.index("--output") + 1]
            dist_root = fixture_app / "dist" / f"phase10-trusted-test-{_BUILD}"
            violations = []
            expected = {
                "bundle": dist_root / "Lulu-Trusted-Test.app",
                "output": dist_root / "candidate-evidence.json",
            }
            for label, value in (("bundle", bundle), ("output", output)):
                if not Path(value).is_absolute():
                    violations.append(f"relative:{label}:{value}")
                elif value != str(expected[label]):
                    violations.append(f"wrong:{label}:{value}")
            self.assertEqual(violations, [])

    def test_fixed_build_python_loads_only_the_local_authority_source(self) -> None:
        self.assertTrue(_FIXED_BUILD_PYTHON.is_file())
        with tempfile.TemporaryDirectory(
            prefix="phase10-local-authority-source-"
        ) as directory:
            root = Path(directory).resolve()
            (
                fixture_app,
                wrapper,
                venv,
                recorder,
                authority_source,
                authority_root,
            ) = _authority_loader_fixture(root)
            third_party_probe = _run(
                [
                    str(_FIXED_BUILD_PYTHON),
                    "-B",
                    "-c",
                    (
                        "import packaging; print(packaging.__file__); "
                        "from packaging.phase10_post_build_authority import "
                        "issue_build_completed_receipt"
                    ),
                ],
                cwd=fixture_app,
            )
            self.assertNotEqual(third_party_probe.returncode, 0)
            self.assertIn("site-packages/packaging", third_party_probe.stdout)
            self.assertIn("ModuleNotFoundError", third_party_probe.stderr)

            result = _run(
                ["/bin/bash", str(wrapper), "--venv", str(venv)],
                cwd=fixture_app,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(recorder.read_text(encoding="utf-8"))
            expected_source = str(authority_source.resolve())
            self.assertEqual(record["module_file"], expected_source)
            self.assertEqual(record["module_origin"], expected_source)
            self.assertEqual(
                record["loader_type"],
                "_frozen_importlib_external.SourceFileLoader",
            )
            self.assertFalse(authority_root.exists())

    def test_wrapper_physical_root_ignores_path_dirname_basename_shims(
        self,
    ) -> None:
        for invocation_mode in ("absolute", "relative"):
            with self.subTest(
                invocation_mode=invocation_mode
            ), tempfile.TemporaryDirectory(
                prefix=f"phase10-path-root-{invocation_mode}-"
            ) as directory:
                root = Path(directory).resolve()
                (
                    real_app,
                    real_wrapper,
                    real_venv,
                    real_recorder,
                    real_authority,
                    _real_authority_root,
                ) = _authority_loader_fixture(root / "real")
                (
                    alternate_app,
                    _alternate_wrapper,
                    _alternate_venv,
                    alternate_recorder,
                    _alternate_authority,
                    alternate_authority_root,
                ) = _authority_loader_fixture(root / "alternate")
                shim_root = root / "path-shims"
                shim_root.mkdir(mode=0o700)
                shim_marker = root / "path-shim-invoked"
                alternate_packaging = alternate_app / "packaging"
                dirname_shim = f"""#!/bin/sh
set -eu
printf 'dirname\\n' >> {shlex.quote(str(shim_marker))}
last=''
for value in "$@"; do last="$value"; done
if [ "$last" = {shlex.quote(str(real_wrapper))} ]; then
  printf '%s\\n' {shlex.quote(str(alternate_packaging))}
elif [ "$last" = {shlex.quote(str(alternate_packaging))} ]; then
  printf '/\\n'
else
  /usr/bin/dirname "$@"
fi
"""
                basename_shim = f"""#!/bin/sh
set -eu
printf 'basename\\n' >> {shlex.quote(str(shim_marker))}
printf 'build_trusted_test.sh\\n'
"""
                _write(
                    shim_root / "dirname",
                    dirname_shim.encode("utf-8"),
                    0o700,
                )
                _write(
                    shim_root / "basename",
                    basename_shim.encode("utf-8"),
                    0o700,
                )
                invocation = (
                    str(real_wrapper)
                    if invocation_mode == "absolute"
                    else "packaging/build_trusted_test.sh"
                )
                result = _run(
                    ["/bin/bash", invocation, "--venv", str(real_venv)],
                    cwd=real_app,
                    env={"PATH": f"{shim_root}:/usr/bin:/bin"},
                )
                violations: list[str] = []
                if result.returncode != 0:
                    violations.append(f"wrapper_exit:{result.returncode}")
                if shim_marker.exists():
                    violations.append("path_shim_executed")
                if not real_recorder.exists():
                    violations.append("physical_authority_not_called")
                if alternate_recorder.exists():
                    violations.append("alternate_authority_called")
                alternate_dist = (
                    alternate_app
                    / "dist"
                    / f"phase10-trusted-test-{_BUILD}"
                )
                if alternate_dist.exists():
                    violations.append("alternate_tree_written")
                if alternate_authority_root.exists():
                    violations.append("alternate_authority_root_created")
                if real_recorder.exists():
                    record = json.loads(real_recorder.read_text(encoding="utf-8"))
                    if record["module_file"] != str(real_authority.resolve()):
                        violations.append("wrong_authority_source")
                self.assertEqual(violations, [])

    def test_trusted_test_wrapper_has_a_fixed_isolated_authority_loader(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (
            app_root / "packaging" / "build_trusted_test.sh"
        ).read_text(encoding="utf-8")
        required = (
            '"$PYTHON_BIN" -E -s -S -',
            "importlib.machinery.SourceFileLoader",
            "importlib.util.spec_from_file_location",
            "phase10_post_build_authority.py",
            "module.__file__",
            "module.__spec__.origin",
            "type(module.__loader__) is importlib.machinery.SourceFileLoader",
        )
        forbidden = (
            "from packaging",
            "import packaging",
            "runpy",
            "PYTHONPATH",
            "PYTHONHOME",
            "HOME=",
            "os.environ",
            "json.loads",
            "sys.argv[",
        )
        violations = [value for value in required if value not in script]
        violations.extend(value for value in forbidden if value in script)
        self.assertEqual(violations, [])

    def test_local_authority_loader_preserves_the_nine_step_opaque_chain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="phase10-authority-chain-"
        ) as directory:
            root = Path(directory).resolve()
            (
                fixture_app,
                wrapper,
                venv,
                recorder,
                _authority_source,
                authority_root,
            ) = _authority_loader_fixture(root)
            result = _run(
                ["/bin/bash", str(wrapper), "--venv", str(venv)],
                cwd=fixture_app,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(recorder.read_text(encoding="utf-8"))
            self.assertEqual(
                record["events"],
                [
                    "issue_build_completed_receipt",
                    "produce_carrier",
                    "review_carrier_security",
                    "review_carrier_test",
                    "finalize_authority_artifact",
                    "review_authority_artifact_security",
                    "review_authority_artifact_test",
                    "issue_post_build_authority",
                    "post_build_public_fingerprint",
                ],
            )
            self.assertFalse(authority_root.exists())

    def test_authority_loader_rejects_or_ignores_caller_authority_inputs(
        self,
    ) -> None:
        cases = {
            "pythonpath_fake_package": {"PYTHONPATH": "{attack}"},
            "pythonhome": {"PYTHONHOME": "{attack}"},
            "caller_authority_values": {
                "LULU_AUTHORITY_PATH": "{attack}/authority.py",
                "LULU_AUTHORITY_MODULE": "packaging.phase10_post_build_authority",
                "LULU_AUTHORITY_JSON": '{"authority":"attacker"}',
                "LULU_AUTHORITY_MAPPING": "attacker",
                "LULU_AUTHORITY_SPEC": "attacker",
                "LULU_AUTHORITY_LOADER": "attacker",
            },
            "preloaded_private_module": {"PYTHONPATH": "{preload}"},
        }
        for label, raw_env in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"authority-input-{label}-"
            ) as directory:
                root = Path(directory).resolve()
                (
                    fixture_app,
                    wrapper,
                    venv,
                    recorder,
                    authority_source,
                    authority_root,
                ) = _authority_loader_fixture(root)
                attack = root / "attacker"
                (attack / "packaging").mkdir(parents=True, mode=0o700)
                attack_marker = root / "attacker-loaded"
                _write(attack / "packaging" / "__init__.py", b"")
                _write(
                    attack / "packaging" / "phase10_post_build_authority.py",
                    (
                        f"from pathlib import Path\n"
                        f"Path({str(attack_marker)!r}).write_text('loaded')\n"
                    ).encode("utf-8"),
                )
                preload = root / "preload"
                preload.mkdir(mode=0o700)
                _write(
                    preload / "sitecustomize.py",
                    b"""import sys
import types
sys.modules['_lulu_phase10_post_build_authority_fixed'] = types.ModuleType('attacker')
""",
                )
                env = {
                    key: value.replace("{attack}", str(attack)).replace(
                        "{preload}", str(preload)
                    )
                    for key, value in raw_env.items()
                }
                result = _run(
                    ["/bin/bash", str(wrapper), "--venv", str(venv)],
                    cwd=fixture_app,
                    env=env,
                )
                if result.returncode == 0:
                    record = json.loads(recorder.read_text(encoding="utf-8"))
                    self.assertEqual(
                        record["module_file"], str(authority_source.resolve())
                    )
                    self.assertEqual(
                        record["module_origin"], str(authority_source.resolve())
                    )
                else:
                    self.assertFalse(recorder.exists())
                self.assertFalse(attack_marker.exists())
                self.assertFalse(authority_root.exists())

    def test_authority_source_path_and_origin_fail_closed_before_carrier(
        self,
    ) -> None:
        cases = (
            "missing",
            "source_symlink",
            "ancestor_symlink",
            "wrong_owner",
            "world_writable",
            "ancestor_world_writable",
            "non_regular",
            "preexisting_private_module",
            "file_origin_drift",
            "spec_origin_drift",
            "loader_type_drift",
        )
        for label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"authority-source-{label}-"
            ) as directory:
                root = Path(directory).resolve()
                (
                    fixture_app,
                    wrapper,
                    venv,
                    recorder,
                    authority_source,
                    authority_root,
                ) = _authority_loader_fixture(root)
                invocation = wrapper
                if label == "missing":
                    authority_source.unlink()
                elif label == "source_symlink":
                    attacker = root / "attacker-authority.py"
                    _write(attacker, authority_source.read_bytes())
                    authority_source.unlink()
                    authority_source.symlink_to(attacker)
                elif label == "ancestor_symlink":
                    linked_workspace = root / "linked-workspace"
                    linked_workspace.symlink_to(root / "workspace", target_is_directory=True)
                    invocation = linked_workspace / "app/packaging/build_trusted_test.sh"
                elif label == "wrong_owner":
                    wrapper_source = wrapper.read_text(encoding="utf-8")
                    self.assertEqual(wrapper_source.count("os.getuid()"), 1)
                    _write(
                        wrapper,
                        wrapper_source.replace(
                            "metadata.st_uid != os.getuid()",
                            "metadata.st_uid != os.getuid() + 1",
                        ).encode("utf-8"),
                        0o700,
                    )
                elif label == "world_writable":
                    os.chmod(authority_source, 0o622)
                elif label == "ancestor_world_writable":
                    os.chmod(authority_source.parent, 0o722)
                elif label == "non_regular":
                    authority_source.unlink()
                    authority_source.mkdir(mode=0o700)
                elif label == "preexisting_private_module":
                    wrapper_source = wrapper.read_text(encoding="utf-8")
                    marker = (
                        '_PRIVATE_MODULE_NAME = '
                        '"_lulu_phase10_post_build_authority_fixed"'
                    )
                    self.assertEqual(wrapper_source.count(marker), 1)
                    _write(
                        wrapper,
                        wrapper_source.replace(
                            marker,
                            marker + "\nsys.modules[_PRIVATE_MODULE_NAME] = object()",
                        ).encode("utf-8"),
                        0o700,
                    )
                elif label == "file_origin_drift":
                    with authority_source.open("a", encoding="utf-8") as handle:
                        handle.write("\n__file__ = '/attacker/authority.py'\n")
                elif label == "spec_origin_drift":
                    with authority_source.open("a", encoding="utf-8") as handle:
                        handle.write("\n__spec__.origin = '/attacker/authority.py'\n")
                elif label == "loader_type_drift":
                    with authority_source.open("a", encoding="utf-8") as handle:
                        handle.write("\n__loader__ = object()\n")

                result = _run(
                    ["/bin/bash", str(invocation), "--venv", str(venv)],
                    cwd=fixture_app,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(recorder.exists())
                self.assertFalse(authority_root.exists())

    def test_candidate_path_negative_matrix_is_zero_write(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(
            str(app_root / "packaging" / "phase10_finalize_candidate.py")
        )
        finalize = namespace["finalize_candidate"]
        CandidateFinalizeError = namespace["CandidateFinalizeError"]
        rejected: list[str] = []

        with tempfile.TemporaryDirectory(
            prefix="candidate-path-negative-"
        ) as directory:
            root = Path(directory).resolve()
            bundle = _fake_bundle(root)
            evidence_root = root / "evidence"
            evidence_root.mkdir(mode=0o700)

            direct_cases = {
                "relative_bundle": (
                    Path("Lulu.app"),
                    evidence_root / "relative-bundle.json",
                ),
                "relative_output": (
                    bundle,
                    Path("TASK-20260822-3FED14-relative-output.json"),
                ),
            }
            linked = root / "linked.app"
            linked.symlink_to(bundle, target_is_directory=True)
            direct_cases["symlink"] = (
                linked,
                evidence_root / "symlink.json",
            )
            for label, (candidate, output) in direct_cases.items():
                runner = _FakeAdHocCodesign()
                with self.subTest(label=label), self.assertRaises(
                    CandidateFinalizeError
                ):
                    finalize(
                        bundle=candidate,
                        bundle_role="dist",
                        output_path=output,
                        codesign_runner=runner,
                    )
                self.assertFalse(output.exists())
                self.assertEqual(runner.calls, [])
                rejected.append(label)

            script = app_root / "packaging" / "build_trusted_test.sh"
            caller_cases = {
                "different_dist_root": [
                    "--dist-root",
                    str(root / "different-dist-root"),
                ],
                "caller_path_substitution": [
                    "--app-path",
                    "relative/Lulu-Trusted-Test.app",
                ],
            }
            for label, arguments in caller_cases.items():
                result = _run(["/bin/bash", str(script), *arguments], cwd=app_root)
                with self.subTest(label=label):
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("用法", result.stderr)
                rejected.append(label)

            self.assertEqual(
                rejected,
                [
                    "relative_bundle",
                    "relative_output",
                    "symlink",
                    "different_dist_root",
                    "caller_path_substitution",
                ],
            )
            self.assertEqual(list(root.rglob("candidate-evidence.json")), [])

    def test_real_profile_gate_failure_becomes_exact_dual_entry_contract(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(str(app_root / "packaging" / "profile_gate.py"))
        verify = namespace["_verify_app_with_reader"]
        with tempfile.TemporaryDirectory(
            prefix="Phase10 profile gate R1 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            app, analysis, archive_reader = _dual_entry_profile_bundle(
                root,
                namespace,
            )
            output = root / "dual-entry-receipt.json"
            codesign_result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                verify.__globals__["subprocess"],
                "run",
                return_value=codesign_result,
            ):
                try:
                    result = verify(
                        app,
                        "external_candidate",
                        analysis,
                        output,
                        archive_reader,
                        macos_compatibility={"ok": True},
                    )
                except SystemExit as exc:
                    red_receipt = json.loads(output.read_text(encoding="utf-8"))
                    self.assertEqual(
                        _profile_gate_failure_projection(red_receipt),
                        _PROFILE_GATE_R1_REAL_FAILURE,
                    )
                    self.fail(f"real dual-entry receipt remains RED: {exc}")

            self.assertTrue(result["ok"])
            self.assertEqual(result["final_findings"], [])
            self.assertEqual(
                result["inventory"]["archive"]["app_entry_scripts"],
                ["run_agent_cli", "run_external"],
            )
            self.assertEqual(
                result["inventory"]["differences"],
                {
                    "modules_missing": [],
                    "modules_extra": [],
                    "resources_missing": [],
                    "resources_extra": [],
                },
            )
            self.assertEqual(
                result["digests"]["analysis_scope_inventory_sha256"],
                result["digests"]["analysis_projection_sha256"],
            )
            self.assertEqual(
                result["digests"]["app_tree_sha256"],
                canonical_app_tree_identity(app)["sha256"],
            )
            entry_records = {
                record["path"]: record
                for record in result["inventory"]["bundle_executables"]
                if record["path"].startswith("Contents/MacOS/")
            }
            self.assertEqual(
                {
                    path: (record["contract"], record["allowed"])
                    for path, record in entry_records.items()
                },
                {
                    "Contents/MacOS/Lulu": ("main_executable", True),
                    "Contents/MacOS/lulu-agent-cli": (
                        "agent_cli_executable",
                        True,
                    ),
                },
            )

            third = app / "Contents" / "MacOS" / "third-tool"
            _write(third, b"\xcf\xfa\xed\xfefake unknown archive", 0o700)
            rejected_output = root / "third-executable-receipt.json"
            with mock.patch.object(
                verify.__globals__["subprocess"],
                "run",
                return_value=codesign_result,
            ), self.assertRaises(SystemExit):
                verify(
                    app,
                    "external_candidate",
                    analysis,
                    rejected_output,
                    archive_reader,
                    macos_compatibility={"ok": True},
                )
            rejected = json.loads(rejected_output.read_text(encoding="utf-8"))
            self.assertEqual(
                rejected["final_findings"],
                [
                    {
                        "kind": "unknown_executable_member",
                        "value": "Contents/MacOS/third-tool",
                    }
                ],
            )
            self.assertFalse(rejected["checks"]["bundle_executable_contract_valid"])

    def test_materialized_zero_symlink_app_repeats_the_complete_profile_gate(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        profile = runpy.run_path(str(app_root / "packaging" / "profile_gate.py"))
        materializer = runpy.run_path(
            str(app_root / "packaging" / "materialize_app_symlinks.py")
        )
        verify = profile["_verify_app_with_reader"]
        materialize = materializer["materialize"]

        with tempfile.TemporaryDirectory(
            prefix="Phase10 materialized final profile 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            app, analysis, archive_reader = _dual_entry_profile_bundle(root, profile)
            resources = app / "Contents/Resources"
            target = resources / "payload-target"
            target.mkdir()
            _write(target / "tool.py", b"#!/usr/bin/env python3\nprint('fixed')\n", 0o700)
            (resources / "payload-alias").symlink_to("payload-target", target_is_directory=True)
            frameworks = app / "Contents/Frameworks"
            frameworks.mkdir(exist_ok=True)
            manifest_alias = frameworks / "agent_product_manifest.json"
            manifest_alias.symlink_to("../Resources/agent_product_manifest.json")

            prior_analysis = json.loads(analysis.read_text(encoding="utf-8"))
            profile_sha = hashlib.sha256(
                (resources / "build_profile.json").read_bytes()
            ).hexdigest()
            profile["write_analysis_receipt"](
                analysis,
                "external_candidate",
                [
                    (name, str(app_root / "src/main.py"))
                    for name in prior_analysis["modules"]
                ],
                profile["_walk_inventory"](resources)[0],
                profile_sha,
            )

            signed_stage = root / "signed-stage-build-profile-receipt.json"
            signed_result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                verify.__globals__["subprocess"], "run", return_value=signed_result
            ):
                first = verify(
                    app,
                    "external_candidate",
                    analysis,
                    signed_stage,
                    archive_reader,
                    macos_compatibility={"ok": True},
                )
            self.assertTrue(first["ok"])
            self.assertEqual(first["app_tree"]["state"], "pending_materialization")

            receipt_path = root / "agent-symlink-materialization-receipt.json"
            receipt = materialize(
                app,
                signed_profile_receipt=signed_stage,
                codesign_runner=lambda _app: signed_result,
            )
            receipt_path.write_text(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(receipt["schema_id"], "lulu.agent-symlink-materialization/v2")
            self.assertEqual(receipt["remaining_symlink_count"], 0)
            self.assertEqual(list(app.rglob("*")) != [], True)
            self.assertFalse(any(path.is_symlink() for path in app.rglob("*")))
            self.assertFalse(manifest_alias.exists())
            manifest_records = [
                record
                for record in receipt["records"]
                if record["path"]
                == "Contents/Frameworks/agent_product_manifest.json"
            ]
            self.assertEqual(
                [record["action"] for record in manifest_records],
                ["omit_self_referential_manifest_alias"],
            )

            # The final closed component manifest is deliberately written after
            # symlink removal. Its PyInstaller Frameworks alias must stay absent,
            # otherwise the manifest would contain a physical copy of itself.
            _write(
                resources / "agent_product_manifest.json",
                b'{"schema_id":"lulu.agent-component-manifest/v1"}\n',
            )

            final_output = root / "build-profile-receipt.json"
            invalid_direct_seal = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="bundle format is ambiguous",
            )
            materialized_validator = profile["_MATERIALIZATION_GATE"][
                "validate_materialized_projection"
            ]
            with mock.patch.object(
                verify.__globals__["subprocess"],
                "run",
                return_value=invalid_direct_seal,
            ), mock.patch.dict(
                materialized_validator.__globals__,
                {"_verify_component_manifest": lambda _app, _manifest: {"state": "ready"}},
            ):
                final = verify(
                    app,
                    "external_candidate",
                    analysis,
                    final_output,
                    archive_reader,
                    macos_compatibility={"ok": True},
                )
            self.assertTrue(final["ok"])
            self.assertTrue(all(final["checks"].values()))
            self.assertEqual(final["final_findings"], [])
            self.assertEqual(final["inventory"]["difference_count"], 0)
            self.assertEqual(
                final["bundle_seal"]["verification"],
                "verified_materialization_of_signed_stage",
            )
            self.assertEqual(
                final["app_tree_binding"]["materialization_receipt_sha256"],
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                final["digests"]["app_tree_sha256"],
                canonical_app_tree_identity(app)["sha256"],
            )

            os.chmod(resources / "payload-alias/tool.py", 0o600)
            with self.assertRaisesRegex(
                (RuntimeError, SystemExit),
                "MATERIALIZED_OBJECT_IDENTITY_DRIFT",
            ):
                verify(
                    app,
                    "external_candidate",
                    analysis,
                    root / "tampered-profile.json",
                    archive_reader,
                    macos_compatibility={"ok": True},
                )

    def test_one_analysis_and_pyz_feed_profile_primary_and_production_console_cli(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        spec_path = app_root / "packaging" / "Lulu.spec"
        source = spec_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(spec_path))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        named = lambda name: [
            call
            for call in calls
            if isinstance(call.func, ast.Name) and call.func.id == name
        ]
        self.assertEqual(len(named("Analysis")), 1)
        self.assertEqual(len(named("PYZ")), 1)
        self.assertEqual(len(named("EXE")), 3)
        self.assertIn("gui_exe = EXE(\n    pyz,", source)
        self.assertIn("agent_cli_exe = EXE(\n        pyz,", source)
        self.assertIn("console=TRUSTED_TEST_SAFE_E2E_ENABLED", source)
        self.assertIn("console=True", source)
        self.assertIn('name="lulu-agent-cli"', source)
        self.assertIn('"LuluBuildIdentity": BUILD_IDENTITY', source)
        self.assertNotIn("Analysis([AGENT_CLI_ENTRY]", source)

    def test_build_script_requires_explicit_identity_and_existing_python311_venv(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (app_root / "packaging" / "build_app.sh").read_text(encoding="utf-8")
        for identity in ("candidate", "trusted-test", "trusted-test-safe-e2e", "production"):
            self.assertIn(identity, script)
        self.assertIn("--identity", script)
        self.assertIn("--venv", script)
        self.assertIn("VENV_PATH", script)
        self.assertIn("python3.11", script)
        for forbidden in (
            "python -m venv",
            "python3 -m venv",
            "pip install",
            "uv sync",
            "curl ",
            "wget ",
        ):
            self.assertNotIn(forbidden, script)
        self.assertNotIn("VENV=.venv/bin", script)
        self.assertIn('"$VENV/python" -m PyInstaller', script)
        self.assertNotIn('"$VENV/pyinstaller"', script)
        self.assertIn("LULU_BUILD_IDENTITY_FOR_SPEC", script)
        self.assertIn("LULU_PRODUCTION_RELEASE_AUTHORIZED", script)
        self.assertIn(
            'MACOSX_DEPLOYMENT_TARGET=15.0 /usr/bin/xcrun clang',
            script,
        )
        self.assertIn('-mmacosx-version-min=15.0', script)
        self.assertIn(
            'PYTHONPATH="$PWD" "$VENV/python" '
            'packaging/finalize_agent_component_manifest.py',
            script,
        )
        prepare_position = script.index(
            "packaging/finalize_agent_component_manifest.py prepare-skill"
        )
        materialize_position = script.index("packaging/materialize_app_symlinks.py")
        codesign_position = script.index("/usr/bin/codesign --force --deep --sign -")
        profile_position = script.index("packaging/profile_gate.py verify-app")
        finalize_position = script.index(
            "packaging/finalize_agent_component_manifest.py finalize-manifest"
        )
        identity_position = script.index(
            "packaging/verify_generation9_candidate_identity.py"
        )
        signed_receipt_position = script.index(
            'cp "$DIST_ROOT/build-profile-receipt.json"'
        )
        final_profile_position = script.index(
            "packaging/profile_gate.py verify-app",
            profile_position + 1,
        )
        self.assertLess(prepare_position, materialize_position)
        self.assertLess(prepare_position, codesign_position)
        self.assertLess(codesign_position, profile_position)
        self.assertLess(profile_position, signed_receipt_position)
        self.assertLess(signed_receipt_position, materialize_position)
        self.assertLess(profile_position, materialize_position)
        self.assertIn("--signed-profile-receipt", script)
        self.assertNotIn("profile_gate.py bind-final-app-tree", script)
        self.assertLess(materialize_position, finalize_position)
        self.assertLess(finalize_position, final_profile_position)
        self.assertLess(final_profile_position, identity_position)

    def test_managed_build_venv_link_is_create_only_and_fail_closed(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (app_root / "packaging" / "build_app.sh").read_text(
            encoding="utf-8"
        )

        with tempfile.TemporaryDirectory(
            prefix="Phase10 venv link 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            _repo, fixture_app = _profile_gate_fixture(root)
            venv = _fake_build_venv(root)
            managed_link = fixture_app / ".venv"
            managed_link.symlink_to(venv, target_is_directory=True)
            valid_command = _profile_create_command(fixture_app, venv)

            missing_argument = list(valid_command)
            flag_index = missing_argument.index("--build-venv-root")
            del missing_argument[flag_index : flag_index + 2]
            missing_result = _run(missing_argument, cwd=fixture_app)
            self.assertNotEqual(missing_result.returncode, 0)
            self.assertIn("--build-venv-root", missing_result.stderr)

            created = _run(valid_command, cwd=fixture_app)
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            profile = json.loads(
                (fixture_app / "build_profile.json").read_text(encoding="utf-8")
            )
            self.assertRegex(profile["source_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(profile["source_tree"], r"^[0-9a-f]{40}$")

            copied_namespace = runpy.run_path(
                str(fixture_app / "packaging" / "profile_gate.py")
            )
            with self.assertRaises(SystemExit):
                copied_namespace["source_identity"](
                    root=fixture_app,
                    allowed_dirty_paths=copied_namespace["_BUILD_GENERATED_PATHS"],
                )

            verify_command = [
                str(venv / "bin" / "python"),
                str(fixture_app / "packaging" / "profile_gate.py"),
                "verify-app",
                "--profile",
                "external_candidate",
                "--app",
                str(root / "missing.app"),
                "--analysis-receipt",
                str(root / "missing-analysis.json"),
                "--output",
                str(root / "missing-output.json"),
                "--build-venv-root",
                str(venv),
            ]
            verify_result = _run(verify_command, cwd=fixture_app)
            self.assertEqual(verify_result.returncode, 2)
            self.assertIn("unrecognized arguments", verify_result.stderr)

            managed_link.unlink()
            other_venv = _fake_build_venv(root / "other")
            managed_link.symlink_to(other_venv, target_is_directory=True)
            wrong_link = _run(valid_command, cwd=fixture_app)
            self.assertNotEqual(wrong_link.returncode, 0)

            managed_link.unlink()
            managed_link.mkdir(mode=0o700)
            ordinary_directory = _run(valid_command, cwd=fixture_app)
            self.assertNotEqual(ordinary_directory.returncode, 0)
            managed_link.rmdir()

            _write(managed_link, b"ordinary file")
            ordinary_file = _run(valid_command, cwd=fixture_app)
            self.assertNotEqual(ordinary_file.returncode, 0)
            managed_link.unlink()
            managed_link.symlink_to(venv, target_is_directory=True)

            os.chmod(venv, 0o720)
            writable_venv = _run(valid_command, cwd=fixture_app)
            self.assertNotEqual(writable_venv.returncode, 0)
            os.chmod(venv, 0o700)

            mismatch_venv = _fake_build_venv(
                root / "mismatch", python=Path("/usr/bin/true")
            )
            managed_link.unlink()
            managed_link.symlink_to(mismatch_venv, target_is_directory=True)
            interpreter_mismatch = _run(
                _profile_create_command(
                    fixture_app,
                    mismatch_venv,
                    executable=Path(sys.executable).resolve(),
                ),
                cwd=fixture_app,
            )
            self.assertNotEqual(interpreter_mismatch.returncode, 0)

            python39_venv = _fake_build_venv(
                root / "python39", python=Path("/usr/bin/python3")
            )
            managed_link.unlink()
            managed_link.symlink_to(python39_venv, target_is_directory=True)
            wrong_python_version = _run(
                _profile_create_command(fixture_app, python39_venv),
                cwd=fixture_app,
            )
            self.assertNotEqual(wrong_python_version.returncode, 0)

            managed_link.unlink()
            managed_link.symlink_to(venv, target_is_directory=True)
            venv_alias = root / "venv alias"
            venv_alias.symlink_to(venv, target_is_directory=True)
            linked_venv_root = _run(
                _profile_create_command(fixture_app, venv_alias),
                cwd=fixture_app,
            )
            self.assertNotEqual(linked_venv_root.returncode, 0)

            missing_venv = root / "missing venv"
            missing_venv_result = _run(
                _profile_create_command(
                    fixture_app,
                    missing_venv,
                    executable=venv / "bin" / "python",
                ),
                cwd=fixture_app,
            )
            self.assertNotEqual(missing_venv_result.returncode, 0)

            live_namespace = runpy.run_path(
                str(app_root / "packaging" / "profile_gate.py")
            )
            validator = live_namespace["managed_build_venv_dirty_path"]
            validator.__globals__["_APP_ROOT"] = fixture_app
            with mock.patch.object(
                validator.__globals__["os"],
                "getuid",
                return_value=os.getuid() + 1,
            ), self.assertRaises(SystemExit):
                validator(str(venv))

        self.assertIn('--build-venv-root "$VENV_PATH"', script)

    def test_build_script_owns_managed_venv_link_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="Phase10 build lifecycle 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()

            _repo, app, venv, support = _build_script_fixture(root / "failure")
            failure_events = root / "failure-events.txt"
            failed = _run_fake_build(
                app,
                venv,
                support,
                event_log=failure_events,
                pyinstaller_mode="fail",
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertTrue(
                failure_events.exists(), failed.stderr or failed.stdout
            )
            self.assertEqual(
                failure_events.read_text(encoding="utf-8").splitlines(),
                ["baseline:absent", "capability:symlink", "pyinstaller:symlink"],
            )
            self.assertFalse(os.path.lexists(app / ".venv"))

            _repo, app, venv, support = _build_script_fixture(root / "success")
            success_events = root / "success-events.txt"
            stopped_before_codesign = _run_fake_build(
                app,
                venv,
                support,
                event_log=success_events,
                pyinstaller_mode="success",
            )
            self.assertNotEqual(stopped_before_codesign.returncode, 0)
            self.assertEqual(
                success_events.read_text(encoding="utf-8").splitlines(),
                [
                    "baseline:absent",
                    "capability:symlink",
                    "pyinstaller:symlink",
                    "find:absent",
                ],
            )
            self.assertFalse(os.path.lexists(app / ".venv"))

            _repo, app, venv, support = _build_script_fixture(root / "preexisting")
            preexisting = app / ".venv"
            _write(preexisting, b"foreign path")
            preexisting_events = root / "preexisting-events.txt"
            rejected = _run_fake_build(
                app,
                venv,
                support,
                event_log=preexisting_events,
                pyinstaller_mode="fail",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(preexisting.read_bytes(), b"foreign path")
            self.assertFalse(preexisting_events.exists())

            preexisting.unlink()
            preexisting.symlink_to(venv, target_is_directory=True)
            linked_events = root / "preexisting-link-events.txt"
            linked_rejected = _run_fake_build(
                app,
                venv,
                support,
                event_log=linked_events,
                pyinstaller_mode="fail",
            )
            self.assertNotEqual(linked_rejected.returncode, 0)
            self.assertTrue(preexisting.is_symlink())
            self.assertEqual(preexisting.resolve(strict=True), venv)
            self.assertFalse(linked_events.exists())

            _repo, app, venv, support = _build_script_fixture(root / "tampered")
            replacement = _fake_build_venv(root / "replacement")
            tampered_events = root / "tampered-events.txt"
            tampered = _run_fake_build(
                app,
                venv,
                support,
                event_log=tampered_events,
                pyinstaller_mode="replace",
                replacement_root=replacement,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertTrue((app / ".venv").is_symlink())
            self.assertEqual((app / ".venv").resolve(strict=True), replacement)

        app_root = Path(__file__).resolve().parents[1]
        script = (app_root / "packaging" / "build_app.sh").read_text(
            encoding="utf-8"
        )
        baseline_position = script.index(
            '"$VENV/python" packaging/product_baseline_gate.py'
        )
        create_position = script.index(
            'profile_gate.py create-managed-venv-link', baseline_position
        )
        capability_position = script.index(
            '"$VENV/python" packaging/product_capability_gate.py'
        )
        pyinstaller_position = script.index('"$VENV/python" -m PyInstaller')
        cleanup_position = script.index(
            "cleanup_managed_venv_link", pyinstaller_position
        )
        verify_position = script.index("packaging/profile_gate.py verify-app")
        self.assertLess(
            baseline_position,
            create_position,
        )
        self.assertLess(create_position, capability_position)
        self.assertLess(capability_position, pyinstaller_position)
        self.assertLess(pyinstaller_position, cleanup_position)
        self.assertLess(cleanup_position, verify_position)

    def test_candidate_build_rejects_uncommitted_source_before_any_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Phase10 dirty source ") as directory:
            root = Path(directory).resolve()
            _repo, app, venv, support = _build_script_fixture(root / "dirty")
            (app / "uncommitted-product.py").write_text("dirty = True\n", encoding="utf-8")
            events = root / "events.txt"
            rejected = _run_fake_build(
                app,
                venv,
                support,
                event_log=events,
                pyinstaller_mode="fail",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("完全干净的 Git worktree", rejected.stderr)
            self.assertFalse(events.exists())
            self.assertFalse(os.path.lexists(app / ".venv"))

    def test_fake_dist_bundle_finalizes_to_candidate_only_evidence(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        finalizer_path = app_root / "packaging" / "phase10_finalize_candidate.py"
        self.assertTrue(finalizer_path.is_file(), "Phase 10 candidate finalizer is missing")
        namespace = runpy.run_path(str(finalizer_path))
        finalize = namespace["finalize_candidate"]
        CandidateFinalizeError = namespace["CandidateFinalizeError"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = _fake_bundle(root)
            output = root / "evidence" / "candidate-evidence.json"
            output.parent.mkdir(mode=0o700)
            runner = _FakeAdHocCodesign()
            result = finalize(
                bundle=bundle,
                bundle_role="dist",
                output_path=output,
                codesign_runner=runner,
            )

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
            self.assertEqual(result["schema_id"], "lulu.phase10-gui-only-candidate-evidence/v1")
            self.assertEqual(result["candidate_identity"], {
                "version": _VERSION,
                "build": _BUILD,
                "channel": "trusted-test",
                "release_state": "trusted_test_non_release",
                "bundle_id": "com.lulu.transcribe.trusted-test",
                "product": "Lulu Trusted Test",
            })
            self.assertEqual(result["bundle_role"], "dist")
            self.assertEqual(result["bundle_path"], str(bundle))
            self.assertEqual(result["package_surface"], "gui_only/v1")
            self.assertEqual(
                {
                    key: result[key]
                    for key in (
                        "diagnostic_contract_id",
                        "diagnostic_only",
                        "promotion_allowed",
                        "retained_install_allowed",
                    )
                },
                {
                    "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
                    "diagnostic_only": True,
                    "promotion_allowed": False,
                    "retained_install_allowed": False,
                },
            )
            self.assertEqual(result["gui_entry"], "Contents/MacOS/Lulu-Trusted-Test")
            self.assertNotIn("adapters", result)
            self.assertEqual(result["signature"], {
                "kind": "ad_hoc",
                "codesign_verified": True,
                "team_id": None,
                "protected_capabilities": "unavailable",
            })
            for field in (
                "bundle_tree_sha256",
                "gui_executable_sha256",
                "build_profile_sha256",
            ):
                self.assertRegex(result[field], r"^[0-9a-f]{64}$")
            self.assertEqual(
                result["gui_executable_sha256"],
                hashlib.sha256(
                    (bundle / "Contents" / "MacOS" / "Lulu-Trusted-Test").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                runner.calls,
                [
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
                    ["/usr/bin/codesign", "--display", "--verbose=4", str(bundle)],
                ],
            )

            malformed = _fake_bundle(root / "malformed")
            (malformed / "Contents" / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleVersion": "wrong"})
            )
            malformed_output = root / "evidence" / "malformed.json"
            unused_runner = _FakeAdHocCodesign()
            with self.assertRaises(CandidateFinalizeError):
                finalize(
                    bundle=malformed,
                    bundle_role="dist",
                    output_path=malformed_output,
                    codesign_runner=unused_runner,
                )
            self.assertFalse(malformed_output.exists())
            self.assertEqual(unused_runner.calls, [])

            linked = root / "linked.app"
            linked.symlink_to(bundle, target_is_directory=True)
            with self.assertRaises(CandidateFinalizeError):
                finalize(
                    bundle=linked,
                    bundle_role="dist",
                    output_path=root / "evidence" / "linked.json",
                    codesign_runner=_FakeAdHocCodesign(),
                )

            with self.assertRaises(CandidateFinalizeError):
                finalize(
                    bundle=Path("Lulu.app"),
                    bundle_role="dist",
                    output_path=root / "evidence" / "relative.json",
                    codesign_runner=_FakeAdHocCodesign(),
                )

    def test_gui_only_finalizer_rejects_every_physical_agent_surface_and_background_marker(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(
            str(app_root / "packaging/phase10_finalize_candidate.py")
        )
        finalize = namespace["finalize_candidate"]
        error = namespace["CandidateFinalizeError"]
        surfaces = (
            "Contents/MacOS/lulu-agent-cli",
            "Contents/Resources/run_agent_cli.py",
            "Contents/Resources/agent_product_manifest.json",
            "Contents/Resources/src/agent_cli/capability_catalog.json",
            "Contents/Resources/skills/lulu-agent/SKILL.md",
            "Contents/Resources/src/agent_cli/gui_host_runtime_hook.py",
            "Contents/Resources/src/core/cli_runtime/factory.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index, relative in enumerate(surfaces):
                with self.subTest(relative=relative):
                    bundle = _fake_bundle(root / str(index))
                    forbidden_path = bundle / relative
                    forbidden_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _write(forbidden_path, b"forbidden\n", 0o700)
                    output = root / f"forbidden-{index}.json"
                    with self.assertRaises(error):
                        finalize(
                            bundle=bundle,
                            bundle_role="dist",
                            output_path=output,
                            codesign_runner=_FakeAdHocCodesign(),
                        )
                    self.assertFalse(output.exists())

            background = _fake_bundle(root / "background")
            info_path = background / "Contents/Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            info["LSBackgroundOnly"] = True
            info_path.write_bytes(plistlib.dumps(info))
            info_path.chmod(0o600)
            with self.assertRaises(error):
                finalize(
                    bundle=background,
                    bundle_role="dist",
                    output_path=root / "background.json",
                    codesign_runner=_FakeAdHocCodesign(),
                )

    def test_candidate_finalizer_accepts_internal_relative_backlinks_and_binds_raw_target(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(
            str(app_root / "packaging" / "phase10_finalize_candidate.py")
        )
        finalize = namespace["finalize_candidate"]

        with tempfile.TemporaryDirectory(
            prefix="Phase10 finalizer symlink 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            digests: list[str] = []
            for label, raw_target in (
                ("direct", "../Resources/AUTHORSHIP.json"),
                (
                    "same-target-different-raw",
                    "../Resources/../Resources/AUTHORSHIP.json",
                ),
            ):
                with self.subTest(label=label):
                    case_root = root / label
                    bundle = _fake_bundle(case_root)
                    frameworks = bundle / "Contents" / "Frameworks"
                    _write(
                        bundle / "Contents" / "Resources" / "AUTHORSHIP.json",
                        b'{"build_profile":"trusted_test"}\n',
                    )
                    link = frameworks / "AUTHORSHIP.json"
                    link.symlink_to(raw_target)
                    output = case_root / "evidence" / "candidate-evidence.json"
                    output.parent.mkdir(mode=0o700)

                    result = finalize(
                        bundle=bundle,
                        bundle_role="dist",
                        output_path=output,
                        codesign_runner=_FakeAdHocCodesign(),
                    )

                    self.assertTrue(output.is_file())
                    self.assertEqual(os.readlink(link), raw_target)
                    self.assertEqual(
                        link.resolve(strict=True),
                        bundle / "Contents" / "Resources" / "AUTHORSHIP.json",
                    )
                    digests.append(result["bundle_tree_sha256"])

            self.assertNotEqual(digests[0], digests[1])

    def test_candidate_finalizer_rejects_absolute_escape_cycle_and_dangling_links_before_evidence(
        self,
    ) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(
            str(app_root / "packaging" / "phase10_finalize_candidate.py")
        )
        finalize = namespace["finalize_candidate"]
        CandidateFinalizeError = namespace["CandidateFinalizeError"]

        with tempfile.TemporaryDirectory(
            prefix="Phase10 finalizer unsafe symlink 中文 空格 "
        ) as directory:
            root = Path(directory).resolve()
            for case in ("absolute", "escape", "cycle", "dangling"):
                with self.subTest(case=case):
                    case_root = root / case
                    bundle = _fake_bundle(case_root)
                    frameworks = bundle / "Contents" / "Frameworks"
                    authorship = (
                        bundle / "Contents" / "Resources" / "AUTHORSHIP.json"
                    )
                    _write(authorship, b"{}\n")
                    if case == "absolute":
                        (frameworks / "AUTHORSHIP.json").symlink_to(authorship)
                    elif case == "escape":
                        outside = case_root / "outside.json"
                        _write(outside, b"outside\n")
                        (frameworks / "AUTHORSHIP.json").symlink_to(
                            "../../../outside.json"
                        )
                    elif case == "cycle":
                        (frameworks / "loop-a").symlink_to("loop-b")
                        (frameworks / "loop-b").symlink_to("loop-a")
                    else:
                        (frameworks / "AUTHORSHIP.json").symlink_to(
                            "missing.json"
                        )
                    output = case_root / "evidence" / "candidate-evidence.json"
                    output.parent.mkdir(mode=0o700)
                    runner = _FakeAdHocCodesign()

                    with self.assertRaises(CandidateFinalizeError):
                        finalize(
                            bundle=bundle,
                            bundle_role="dist",
                            output_path=output,
                            codesign_runner=runner,
                        )
                    self.assertFalse(output.exists())
                    self.assertEqual(runner.calls, [])

    def test_explicit_phase10_build_and_dist_roots_are_owned_and_workspace_bound(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        script = (app_root / "packaging" / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("LULU_BUILD_ROOT", script)
        self.assertIn("LULU_DIST_ROOT", script)
        self.assertIn('--workpath "$BUILD_ROOT"', script)
        self.assertIn('--distpath "$DIST_ROOT"', script)
        self.assertNotIn('WORK_DIR="build/${BUILD_IDENTITY}"', script)
        self.assertNotIn('DIST_DIR="dist/${BUILD_IDENTITY}"', script)

        helper_path = app_root / "packaging" / "phase10_prepare_build_roots.py"
        self.assertTrue(helper_path.is_file(), "Phase 10 build-root helper is missing")
        namespace = runpy.run_path(str(helper_path))
        prepare = namespace["prepare_phase10_build_roots"]
        BuildRootContractError = namespace["BuildRootContractError"]

        with tempfile.TemporaryDirectory(prefix="Phase10 roots 中文 空格 ") as directory:
            fake_app = Path(directory).resolve() / "workspace" / "app"
            fake_app.mkdir(parents=True, mode=0o700)
            build_parent = fake_app / "build"
            dist_parent = fake_app / "dist"
            build_parent.mkdir(mode=0o700)
            dist_parent.mkdir(mode=0o700)
            build_root = build_parent / "phase10-candidate-20260823.1"
            dist_root = dist_parent / "phase10-candidate-20260823.1"

            prepared = prepare(
                app_root=fake_app,
                build_root=build_root,
                dist_root=dist_root,
                identity="candidate",
                version="0.3.2",
                build="20260823.1",
            )
            self.assertEqual(prepared["build_root"], str(build_root))
            self.assertEqual(prepared["dist_root"], str(dist_root))
            self.assertEqual(prepared["owner"], "lulu.phase10-build-root/v1")
            for role, root in (("build", build_root), ("dist", dist_root)):
                marker = json.loads(
                    (root / ".lulu-phase10-root.json").read_text(encoding="utf-8")
                )
                self.assertEqual(marker["role"], role)
                self.assertEqual(marker["root_path"], str(root))
                self.assertEqual(marker["candidate_identity"], {
                    "identity": "candidate",
                    "version": "0.3.2",
                    "build": "20260823.1",
                })

            _write(build_root / "owned-artifact.txt", b"owned")
            prepare(
                app_root=fake_app,
                build_root=build_root,
                dist_root=dist_root,
                identity="candidate",
                version="0.3.2",
                build="20260823.1",
            )

            foreign = dist_parent / "foreign"
            foreign.mkdir(mode=0o700)
            _write(foreign / "unowned.txt", b"foreign")
            rejected = (
                {"build_root": Path("build/relative")},
                {"build_root": fake_app / "outside" / build_root.name},
                {"dist_root": foreign},
                {"dist_root": build_root},
                {"build_root": build_parent / "wrong-name"},
            )
            for replacement in rejected:
                arguments = {
                    "app_root": fake_app,
                    "build_root": build_root,
                    "dist_root": dist_root,
                    "identity": "candidate",
                    "version": "0.3.2",
                    "build": "20260823.1",
                    **replacement,
                }
                with self.subTest(replacement=replacement), self.assertRaises(
                    BuildRootContractError
                ):
                    prepare(**arguments)

            foreign_app = Path(directory).resolve() / "foreign-workspace" / "app"
            (foreign_app / "build").mkdir(parents=True, mode=0o700)
            (foreign_app / "dist").mkdir(mode=0o700)
            foreign_build = foreign_app / "build" / build_root.name
            foreign_dist = foreign_app / "dist" / dist_root.name
            foreign_dist.mkdir(mode=0o700)
            _write(foreign_dist / "unowned.txt", b"foreign")
            with self.assertRaises(BuildRootContractError):
                prepare(
                    app_root=foreign_app,
                    build_root=foreign_build,
                    dist_root=foreign_dist,
                    identity="candidate",
                    version="0.3.2",
                    build="20260823.1",
                )
            self.assertEqual((foreign_dist / "unowned.txt").read_bytes(), b"foreign")

            linked_app = Path(directory).resolve() / "linked-workspace" / "app"
            (linked_app / "build").mkdir(parents=True, mode=0o700)
            (linked_app / "dist").mkdir(mode=0o700)
            linked_build = linked_app / "build" / build_root.name
            linked_dist = linked_app / "dist" / dist_root.name
            linked_dist.symlink_to(dist_root, target_is_directory=True)
            with self.assertRaises(BuildRootContractError):
                prepare(
                    app_root=linked_app,
                    build_root=linked_build,
                    dist_root=linked_dist,
                    identity="candidate",
                    version="0.3.2",
                    build="20260823.1",
                )


if __name__ == "__main__":
    unittest.main()
