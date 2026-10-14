#!/usr/bin/env python3
"""Final-artifact build-profile and PyInstaller inventory gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import marshal
import os
from pathlib import Path
import plistlib
import re
import runpy
import stat
import struct
import subprocess
import sys
import tempfile
from types import CodeType


_APP_ROOT = Path(__file__).resolve().parents[1]
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

try:
    from src.core.agent_entry.canonical_app_tree import (
        CanonicalAppTreeError,
        canonical_app_tree_identity,
    )
except ModuleNotFoundError:
    # The create/link-management commands are intentionally usable by the
    # hermetic build harness before product modules are staged. Verification
    # below still fails closed if this one normative module is unavailable.
    CanonicalAppTreeError = RuntimeError
    canonical_app_tree_identity = None


PROFILES = {
    "internal_test": {
        "app_name": "Lulu Internal Test",
        "bundle_id": "com.lulu.transcribe.internal-test",
        "artifact_prefix": "Lulu-Internal-Test",
        "upload_target": "none",
        "entry": "run_internal.py",
        "required_modules": (
            "src.internal_main",
            "src.ui.main_window",
            "src.core.pipeline",
        ),
    },
    "external_candidate": {
        "app_name": "Lulu",
        "bundle_id": "com.lulu.transcribe",
        "artifact_prefix": "Lulu",
        "upload_target": "github-release-candidate",
        "entry": "run_external.py",
        "required_modules": (
            "src.main",
            "src.ui.main_window",
            "src.core.pipeline",
            "src.core.auth_guard",
        ),
    },
    "trusted_test": {
        "app_name": "Lulu Trusted Test",
        "bundle_id": "com.lulu.transcribe.trusted-test",
        "artifact_prefix": "Lulu-Trusted-Test",
        "upload_target": "none",
        "entry": "run_trusted_test.py",
        "package_surface": "gui_only/v1",
        "required_modules": (
            "src.trusted_main",
            "src.release_features",
            "src.ui.main_window",
            "src.core.pipeline",
        ),
    },
    "trusted_test_safe_e2e": {
        "app_name": "Lulu Trusted Test Safe E2E",
        "bundle_id": "com.lulu.transcribe.trusted-test.safe-e2e",
        "artifact_prefix": "Lulu-Trusted-Test-Safe-E2E",
        "upload_target": "none",
        "entry": "run_trusted_test_safe_e2e.py",
        "package_surface": "isolated_local_transcription_e2e/v1",
        "required_modules": (
            "src.trusted_test_safe_e2e",
            "src.core.pipeline",
            "src.core.transcription_job_application",
        ),
    },
}

APPROVED_SECONDARY_ENTRY_CONTRACTS = {
    "external_candidate": {
        "module": "run_agent_cli",
        "relative_path": "Contents/MacOS/lulu-agent-cli",
        "contract": "agent_cli_executable",
    },
}
APPROVED_TERTIARY_ENTRY_CONTRACTS = {
    "external_candidate": {
        "module": "run_agent_core",
        "relative_path": "Contents/MacOS/lulu-agent-core",
        "contract": "agent_core_executable",
    },
}

_PROFILE_RUNTIME_HOOK_SCRIPTS = {
    "internal_test": frozenset(),
    "trusted_test": frozenset(),
    "trusted_test_safe_e2e": frozenset(),
    "external_candidate": frozenset({"gui_host_runtime_hook"}),
}
_FIXED_RUNTIME_HOOK_SCRIPTS = frozenset().union(
    *_PROFILE_RUNTIME_HOOK_SCRIPTS.values()
)

_EXTERNAL_FORBIDDEN_MODULES = (
    "src.core.license._service_core",
    "src.core.license.external_service",
    "src.core.license.core_service",
    "src.core.license.transport",
    "src.core.license.service",
    "src.core.license.device",
    "src.core.cli_runtime.license_projection",
    "src.ui.license_gate",
    "src.ui.license_device_page",
    "src.ui.license_contact",
    "src.internal_bootstrap",
    "src.internal_main",
    "src.core.license.mock_transport",
    "run_app",
    "run_internal",
    "run_trusted_test_safe_e2e",
    "src.trusted_test_safe_e2e",
)
_EXTERNAL_FORBIDDEN_MARKERS = (
    b"LULU_SELFTEST",
    b"LULU_SELFCHECK",
    b"LULU_TTS_SMOKE",
    b"LULU-MOCK-",
    b"MockLicenseTransport",
    b"mock_transport",
    b"internal_test",
    b"run_internal",
    b"run_trusted_test_safe_e2e",
    b"trusted_test_safe_e2e",
    b"internalTestBadge",
    b"/private/tmp/lulu-internal-test",
    "内部测试版".encode("utf-8"),
)
_APP_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_IDENTITY_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRODUCT_SOURCE_RESOURCE = "product_source.json"
_PRODUCT_SOURCE_RESOURCES = (
    "THIRD_PARTY_NOTICES_DOUYIN_COLLECT.md",
    "THIRD_PARTY_NOTICES_LIVE_RECORDING.md",
    "src/core/live_recording/STREAMGET_LICENSE.txt",
    "THIRD_PARTY_NOTICES_VOXCPM2.md",
    "src/core/synthesis/indextts2_worker.py",
    "src/core/synthesis/voxcpm2_worker.py",
    "src/core/synthesis/qwen3_worker.py",
)
_BUILD_GENERATED_PATHS = frozenset(
    {
        "app/build_profile.json",
        "app/license_runtime_config.json",
        "app/product_source.json",
        "app/release_features.json",
        "app/update_public_key.pem",
        "app/packaging/AUTHORSHIP.json",
    }
)
_EXTERNAL_ALLOWED_PROJECT_MARKERS = {
    # 正式采集 registry 需要按已封存 build profile 关闭或开放能力；这里只允许
    # profile 名本身，不允许任何内部入口、mock transport 或内部 UI 标记。
    "src.core.collect.adapters": {b"internal_test"},
}
_INTERNAL_FORBIDDEN_MODULE_PREFIXES = (
    "src.core.license._service_core",
    "src.core.license.external_service",
    "src.core.license.service",
    "src.core.license.store",
    "src.core.license.transport",
    "src.core.license.mock_transport",
    "keyring",
)
_INTERNAL_FORBIDDEN_RESOURCES = (
    "license_runtime_config.json",
    "update_public_key.pem",
    "update_public_keys.json",
)
_TRUSTED_FORBIDDEN_MODULE_PREFIXES = (
    "src.main",
    "src.internal_bootstrap",
    "src.internal_main",
    "src.ui.license_gate",
    "src.core.license.external_service",
    "src.core.license.mock_transport",
    "src.core.license.service",
    "run_app",
    "run_external",
    "run_internal",
    "run_agent_cli",
    "src.agent_cli",
    "src.core.cli_runtime",
    "src.core.license.store",
    "keyring",
)
_TRUSTED_FORBIDDEN_RESOURCES = (
    "license_runtime_config.json",
    "update_public_key.pem",
    "update_public_keys.json",
    "agent_product_manifest.json",
    "capability_catalog.json",
    "gui_host_runtime_hook.py",
    "run_agent_cli.py",
)
_TRUSTED_FORBIDDEN_RESOURCE_PARTS = (
    "skills/lulu-agent",
    "src/agent_cli",
    "src/core/cli_runtime",
)
_SAFE_E2E_FORBIDDEN_MODULE_PREFIXES = (
    "src.main",
    "src.trusted_main",
    "src.internal_bootstrap",
    "src.internal_main",
    "src.ui",
    "src.core.license.external_service",
    "src.core.license.mock_transport",
    "src.core.license.service",
    "src.core.license.store",
    "src.core.cli_runtime",
    "src.agent_cli",
    "run_agent_cli",
    "run_external",
    "run_internal",
    "run_trusted_test",
    "keyring",
)
_SAFE_E2E_FORBIDDEN_RESOURCES = _TRUSTED_FORBIDDEN_RESOURCES + (
    "release_features.json",
    "run_trusted_test.py",
)
_RELEASE_FEATURES = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "src" / "release_features.py")
)
_PRIVACY_GATE = runpy.run_path(str(Path(__file__).resolve().parent / "privacy_gate.py"))
_MACOS_COMPATIBILITY_GATE = runpy.run_path(
    str(Path(__file__).resolve().parent / "macos_compatibility.py")
)
_MATERIALIZATION_GATE = runpy.run_path(
    str(Path(__file__).resolve().parent / "materialize_app_symlinks.py")
)

_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
_ALLOWED_FRAMEWORK_TOOLS = (
    re.compile(r"^Contents/Frameworks/(?:PySide6/Qt/lib/)?QtWebEngineCore\.framework/Versions/A/Helpers/QtWebEngineProcess\.app/Contents/MacOS/QtWebEngineProcess$"),
    re.compile(r"^Contents/Frameworks/download_runtime/deno$"),
    re.compile(r"^Contents/Frameworks/imageio_ffmpeg/binaries/ffmpeg-[^/]+$"),
    re.compile(r"^Contents/Frameworks/torch/bin/(?:protoc(?:-[0-9.]+)?|torch_shm_manager)$"),
)
_ALLOWED_QT_FRAMEWORK_BINARY = re.compile(
    r"^Contents/Frameworks/.+\.framework/Versions/[^/]+/([^/]+)$"
)
_PYINSTALLER_BOOTSTRAP_MODULES = {
    "pyiboot01_bootstrap",
    "pyimod01_archive",
    "pyimod02_importers",
    "pyimod03_ctypes",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _canonical_digest(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normal_path(value: str) -> str:
    normalized = str(value).replace(os.sep, "/").lstrip("./")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _validated_source_identity(source_commit: str, source_tree: str) -> tuple[str, str]:
    commit = str(source_commit)
    tree = str(source_tree)
    if not _SOURCE_IDENTITY_RE.fullmatch(commit) or not _SOURCE_IDENTITY_RE.fullmatch(tree):
        raise ValueError("source_commit/source_tree 必须是40位小写十六进制Git对象")
    return commit, tree


def _validated_sha256(value: str, label: str) -> str:
    digest = str(value)
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{label}必须是64位小写十六进制SHA-256")
    return digest


def _git_command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _git_blob(root: Path, tree: str, repo_path: str) -> bytes:
    if not _SOURCE_IDENTITY_RE.fullmatch(str(tree)):
        raise SystemExit("Git source tree 格式无效")
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), "show", f"{tree}:{repo_path}"],
        capture_output=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )
    if result.returncode != 0:
        raise SystemExit(f"固定Git tree缺少产品源文件:{repo_path}")
    return result.stdout


def _module_source_path(module: str) -> str:
    if module.startswith("run_"):
        return f"app/{module}.py"
    if module == "src":
        return "app/src/__init__.py"
    if not module.startswith("src."):
        raise ValueError(f"非Lulu产品模块:{module}")
    return f"app/{module.replace('.', '/')}.py"


def _framed(tag: bytes, payload: bytes = b"") -> bytes:
    return (
        len(tag).to_bytes(2, "big")
        + tag
        + len(payload).to_bytes(8, "big")
        + payload
    )


def _stable_code_value(value) -> bytes:
    """Return a deterministic semantic encoding for Python code constants.

    Raw marshal bytes are deliberately excluded: marshal preserves reference
    sharing as an implementation detail, so compile -> dump and
    dump -> load -> dump can differ for the same code object.
    """

    if value is None:
        return _framed(b"none")
    if value is Ellipsis:
        return _framed(b"ellipsis")
    if type(value) is bool:
        return _framed(b"bool", b"1" if value else b"0")
    if type(value) is int:
        return _framed(b"int", str(value).encode("ascii"))
    if type(value) is float:
        return _framed(b"float", struct.pack(">d", value))
    if type(value) is complex:
        return _framed(
            b"complex",
            struct.pack(">d", value.real) + struct.pack(">d", value.imag),
        )
    if type(value) is str:
        return _framed(b"str", value.encode("utf-8"))
    if type(value) is bytes:
        return _framed(b"bytes", value)
    if type(value) is tuple:
        return _framed(
            b"tuple",
            b"".join(_framed(b"item", _stable_code_value(item)) for item in value),
        )
    if type(value) is frozenset:
        return _framed(
            b"frozenset",
            b"".join(
                _framed(b"item", encoded)
                for encoded in sorted(_stable_code_value(item) for item in value)
            ),
        )
    if isinstance(value, CodeType):
        fields = (
            ("argcount", value.co_argcount),
            ("posonlyargcount", value.co_posonlyargcount),
            ("kwonlyargcount", value.co_kwonlyargcount),
            ("nlocals", value.co_nlocals),
            ("stacksize", value.co_stacksize),
            ("flags", value.co_flags),
            ("code", value.co_code),
            ("consts", value.co_consts),
            ("names", value.co_names),
            ("varnames", value.co_varnames),
            ("filename", value.co_filename),
            ("name", value.co_name),
            ("qualname", value.co_qualname),
            ("firstlineno", value.co_firstlineno),
            ("linetable", value.co_linetable),
            ("exceptiontable", value.co_exceptiontable),
            ("freevars", value.co_freevars),
            ("cellvars", value.co_cellvars),
        )
        return _framed(
            b"code-object",
            b"".join(
                _framed(name.encode("ascii"), _stable_code_value(field_value))
                for name, field_value in fields
            ),
        )
    raise TypeError(f"不支持的代码常量类型:{type(value).__name__}")


def _stable_code_sha256(code: CodeType) -> str:
    return hashlib.sha256(_stable_code_value(code)).hexdigest()


def _code_sha256(source: bytes, repo_path: str) -> str:
    try:
        code = compile(source, repo_path.removeprefix("app/"), "exec", dont_inherit=True, optimize=0)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"固定Git产品源码无法编译:{repo_path}") from exc
    return _stable_code_sha256(code)


def product_source_manifest(
    profile: str,
    *,
    source_commit: str,
    source_tree: str,
    source_loader,
) -> dict:
    expected = PROFILES.get(profile)
    if expected is None:
        raise ValueError("未知 build profile")
    commit, tree = _validated_source_identity(source_commit, source_tree)
    entry_module = Path(expected["entry"]).stem
    modules = (entry_module, *expected["required_modules"])
    module_records = {}
    for module in modules:
        repo_path = _module_source_path(module)
        raw = source_loader(repo_path)
        module_records[module] = {
            "path": repo_path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "code_sha256": _code_sha256(raw, repo_path),
        }
    spec_path = "app/packaging/Lulu.spec"
    spec_raw = source_loader(spec_path)
    resource_records = {}
    for bundle_path in _PRODUCT_SOURCE_RESOURCES:
        repo_path = f"app/{bundle_path}"
        raw = source_loader(repo_path)
        resource_records[bundle_path] = {
            "path": repo_path,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return {
        "schema": 1,
        "profile": profile,
        "source_commit": commit,
        "source_tree": tree,
        "entry_module": entry_module,
        "required_modules": list(expected["required_modules"]),
        "modules": module_records,
        "resources": resource_records,
        "spec": {
            "path": spec_path,
            "sha256": hashlib.sha256(spec_raw).hexdigest(),
        },
    }


def product_source_manifest_from_git(
    profile: str,
    source_commit: str,
    source_tree: str,
    *,
    root: Path = _APP_ROOT,
) -> dict:
    return product_source_manifest(
        profile,
        source_commit=source_commit,
        source_tree=source_tree,
        source_loader=lambda repo_path: _git_blob(root, source_tree, repo_path),
    )


def product_source_sha256(payload: dict) -> str:
    return _canonical_digest(payload)


def write_product_source(path: Path, payload: dict) -> str:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return product_source_sha256(payload)


def source_identity(
    root: Path = _APP_ROOT,
    *,
    require_clean: bool = True,
    allowed_dirty_paths: frozenset[str] = frozenset(),
) -> tuple[str, str]:
    root = root.resolve()
    commit_result = _git_command(root, "rev-parse", "--verify", "HEAD^{commit}")
    tree_result = _git_command(root, "rev-parse", "--verify", "HEAD^{tree}")
    if commit_result.returncode != 0 or tree_result.returncode != 0:
        raise SystemExit("无法从当前Git对象取得构建来源")
    try:
        commit, tree = _validated_source_identity(
            commit_result.stdout.strip(),
            tree_result.stdout.strip(),
        )
    except ValueError as exc:
        raise SystemExit("Git构建来源格式无效") from exc
    if require_clean:
        status = _git_command(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
        )
        if status.returncode != 0:
            raise SystemExit("无法核验构建源码状态")
        dirty_paths = {
            line[3:]
            for line in status.stdout.splitlines()
            if len(line) > 3
        }
        if dirty_paths - set(allowed_dirty_paths):
            raise SystemExit("App构建源码存在未提交改动，拒绝生成来源封印")
    return commit, tree


def _validated_build_venv_root(build_venv_root: str) -> Path:
    declared_root = Path(build_venv_root)
    if not declared_root.is_absolute():
        raise SystemExit("build venv root 必须是显式绝对路径")
    try:
        root_stat = declared_root.lstat()
        root_real = declared_root.resolve(strict=True)
        executable_real = Path(sys.executable).resolve(strict=True)
        expected_python_real = (declared_root / "bin" / "python").resolve(
            strict=True
        )
    except OSError as exc:
        raise SystemExit("build venv root 或解释器缺失") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_real != declared_root
    ):
        raise SystemExit("build venv root 必须是非符号链接的真实绝对目录")
    if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o022:
        raise SystemExit("build venv root 必须归当前用户所有且不可由 group/world 写入")
    if executable_real != expected_python_real:
        raise SystemExit("当前 Python 不是显式 build venv root 的 bin/python")
    if sys.version_info[:2] != (3, 11):
        raise SystemExit("build venv 必须使用 Python 3.11")
    return declared_root


def managed_build_venv_dirty_path(build_venv_root: str) -> str:
    """Validate the one build-venv link that create may exclude from dirty paths."""

    declared_root = _validated_build_venv_root(build_venv_root)
    managed_link = _APP_ROOT / ".venv"
    try:
        managed_mode = managed_link.lstat().st_mode
        managed_real = managed_link.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("build venv link 缺失，拒绝放宽源码封印") from exc
    if not stat.S_ISLNK(managed_mode):
        raise SystemExit("app/.venv 必须是受管符号链接")
    if managed_real != declared_root:
        raise SystemExit("app/.venv 未精确指向显式 build venv root")
    return "app/.venv"


def _managed_venv_link_token(link_stat: os.stat_result) -> str:
    return f"{link_stat.st_dev}:{link_stat.st_ino}:{link_stat.st_ctime_ns}"


def create_managed_build_venv_link(build_venv_root: str) -> str:
    declared_root = _validated_build_venv_root(build_venv_root)
    managed_link = _APP_ROOT / ".venv"
    try:
        managed_link.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SystemExit("无法确认 app/.venv 是否存在") from exc
    else:
        raise SystemExit("app/.venv 已存在，拒绝覆盖")
    try:
        os.symlink(str(declared_root), managed_link, target_is_directory=True)
    except OSError as exc:
        raise SystemExit("无法创建受管 app/.venv 链接") from exc
    created_stat = managed_link.lstat()
    created_token = _managed_venv_link_token(created_stat)
    try:
        managed_build_venv_dirty_path(str(declared_root))
        if _managed_venv_link_token(managed_link.lstat()) != created_token:
            raise SystemExit("刚创建的 app/.venv 链接已被替换")
        return created_token
    except BaseException:
        try:
            link_stat = managed_link.lstat()
            if (
                stat.S_ISLNK(link_stat.st_mode)
                and _managed_venv_link_token(link_stat) == created_token
                and managed_link.resolve(strict=True) == declared_root
            ):
                managed_link.unlink()
        except OSError:
            pass
        raise


def remove_managed_build_venv_link(
    build_venv_root: str,
    link_token: str,
) -> None:
    declared_root = _validated_build_venv_root(build_venv_root)
    managed_link = _APP_ROOT / ".venv"
    try:
        link_stat = managed_link.lstat()
        managed_real = managed_link.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("受管 app/.venv 链接缺失或已损坏，保留现场") from exc
    if not stat.S_ISLNK(link_stat.st_mode):
        raise SystemExit("app/.venv 已被替换为未知对象，保留现场")
    if _managed_venv_link_token(link_stat) != link_token:
        raise SystemExit("app/.venv 已被替换，保留现场")
    if managed_real != declared_root:
        raise SystemExit("app/.venv 已改指其他路径，保留现场")
    managed_link.unlink()


def profile_payload(
    profile: str,
    *,
    version: str,
    build: str,
    source_commit: str,
    source_tree: str,
    product_source_sha256: str = "0" * 64,
) -> dict:
    if profile not in PROFILES:
        raise ValueError("build profile 必须显式为 internal_test、trusted_test、trusted_test_safe_e2e 或 external_candidate")
    commit, tree = _validated_source_identity(source_commit, source_tree)
    source_digest = _validated_sha256(
        product_source_sha256,
        "product_source_sha256",
    )
    return {
        "schema": 1,
        "profile": profile,
        "version": version,
        "build": build,
        "source_commit": commit,
        "source_tree": tree,
        "product_source_sha256": source_digest,
        **{
            key: value
            for key, value in PROFILES[profile].items()
            if key != "required_modules"
        },
    }


def write_profile(
    path: Path,
    profile: str,
    *,
    version: str,
    build: str,
    source_commit: str,
    source_tree: str,
    product_source_sha256: str = "0" * 64,
) -> dict:
    payload = profile_payload(
        profile,
        version=version,
        build=build,
        source_commit=source_commit,
        source_tree=source_tree,
        product_source_sha256=product_source_sha256,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return payload


def _forbidden_module(profile: str, name: str) -> bool:
    if profile == "external_candidate":
        return name in _EXTERNAL_FORBIDDEN_MODULES or any(name.startswith(item + ".") for item in _EXTERNAL_FORBIDDEN_MODULES)
    if profile == "trusted_test":
        return any(name == prefix or name.startswith(prefix + ".") for prefix in _TRUSTED_FORBIDDEN_MODULE_PREFIXES)
    if profile == "trusted_test_safe_e2e":
        return any(name == prefix or name.startswith(prefix + ".") for prefix in _SAFE_E2E_FORBIDDEN_MODULE_PREFIXES)
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _INTERNAL_FORBIDDEN_MODULE_PREFIXES)


def _trusted_forbidden_resource(name: str) -> bool:
    normalized = _normal_path(name).strip("/")
    return (
        Path(normalized).name in _TRUSTED_FORBIDDEN_RESOURCES
        or any(
            normalized == part
            or normalized.startswith(part + "/")
            or f"/{part}/" in f"/{normalized}/"
            for part in _TRUSTED_FORBIDDEN_RESOURCE_PARTS
        )
    )


def _safe_e2e_forbidden_resource(name: str) -> bool:
    normalized = _normal_path(name).strip("/")
    return (
        Path(normalized).name in _SAFE_E2E_FORBIDDEN_RESOURCES
        or any(
            normalized == part
            or normalized.startswith(part + "/")
            or f"/{part}/" in f"/{normalized}/"
            for part in _TRUSTED_FORBIDDEN_RESOURCE_PARTS
        )
    )


def _marker_hits(label: str, raw: bytes, markers: tuple[bytes, ...]) -> list[dict[str, str]]:
    return [
        {"kind": "content_marker", "value": f"{label}:{marker.decode('utf-8', 'replace')}"}
        for marker in markers
        if marker in raw
    ]


def _external_module_markers(name: str) -> tuple[bytes, ...]:
    """Return content markers only for Lulu-owned entry/source modules."""

    if name != "run_external" and not name.startswith("src."):
        return ()
    allowed = _EXTERNAL_ALLOWED_PROJECT_MARKERS.get(name, set())
    return tuple(marker for marker in _EXTERNAL_FORBIDDEN_MARKERS if marker not in allowed)


def _project_source_markers(name: str, path: Path) -> tuple[bytes, ...]:
    """Return markers that remain meaningful for Lulu-owned Python sources."""

    try:
        relative = path.resolve().relative_to(_APP_ROOT)
    except (OSError, ValueError):
        # PyInstaller Analysis also exposes third-party source paths. Generic
        # substrings in those dependencies do not prove that a Lulu internal
        # entry point was bundled; module/resource inventory checks remain in
        # force for every collected member.
        return ()
    if relative.parent != Path(".") and (not relative.parts or relative.parts[0] != "src"):
        # The virtual environment also lives below app/. Only Lulu's entry
        # modules and app/src are project-owned source for this content gate.
        return ()
    return _external_module_markers(name)


def _resource_module_name(name: str) -> str | None:
    """Map bundled Lulu source resources back to their Python module name."""

    normalized = _normal_path(name)
    if not normalized.startswith("src/") or not normalized.endswith(".py"):
        return None
    parts = normalized[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _file_marker_hits(path: Path, label: str, markers: tuple[bytes, ...]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    pending = set(markers)
    overlap = b""
    with path.open("rb") as handle:
        while pending:
            block = handle.read(1024 * 1024)
            if not block:
                break
            data = overlap + block
            matched = {marker for marker in pending if marker in data}
            findings.extend(
                {"kind": "executable_content_marker", "value": f"{label}:{marker.decode('utf-8', 'replace')}"}
                for marker in sorted(matched)
            )
            pending -= matched
            overlap = data[-128:]
    return findings


def _allowed_nested_macho(rel: str) -> bool:
    if not rel.startswith("Contents/Frameworks/"):
        return False
    if rel.endswith((".so", ".dylib")):
        return True
    framework_match = _ALLOWED_QT_FRAMEWORK_BINARY.fullmatch(rel)
    if framework_match:
        framework_dir = rel.split(".framework/", 1)[0].rsplit("/", 1)[-1]
        return framework_match.group(1) == framework_dir
    return any(pattern.fullmatch(rel) for pattern in _ALLOWED_FRAMEWORK_TOOLS)


def _bundle_executable_inventory(
    app: Path,
    approved_executables: dict[str, str],
    profile: str,
    declared_resources: set[str],
    materialized_alias_roots: tuple[str, ...] = (),
) -> tuple[list[dict], list[dict[str, str]], str]:
    records: list[dict] = []
    findings: list[dict[str, str]] = []
    main_rel = next(
        rel
        for rel, contract in approved_executables.items()
        if contract == "main_executable"
    )

    for current, dirs, files in os.walk(app, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(name for name in dirs if not (current_path / name).is_symlink())
        for name in sorted(files):
            path = current_path / name
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                continue
            with path.open("rb") as handle:
                prefix = handle.read(4)
            rel = path.relative_to(app).as_posix()
            executable_mode = bool(st.st_mode & 0o111)
            macho = prefix in _MACHO_MAGICS
            script = prefix.startswith(b"#!")
            if not (executable_mode or macho or script):
                continue

            allowed = False
            contract = "unknown"
            if rel in approved_executables:
                allowed = True
                contract = approved_executables[rel]
            elif macho and _allowed_nested_macho(rel):
                allowed = True
                contract = "framework_macho"
            elif (
                (executable_mode or script)
                and not macho
                and rel.startswith("Contents/Resources/")
                and rel.removeprefix("Contents/Resources/") in declared_resources
            ):
                allowed = True
                contract = "declared_resource_script" if script else "declared_non_entry_resource"
            elif any(
                rel == root or rel.startswith(root + "/")
                for root in materialized_alias_roots
            ):
                allowed = True
                contract = "verified_materialized_alias"

            kind = "macho" if macho else "script" if script else "executable_mode"
            record = {
                "path": rel,
                "kind": kind,
                "mode": stat.S_IMODE(st.st_mode),
                "size": st.st_size,
                "sha256": _sha256(path),
                "contract": contract,
                "allowed": allowed,
            }
            records.append(record)
            if profile in {"external_candidate", "trusted_test", "trusted_test_safe_e2e"}:
                markers = (
                    _EXTERNAL_FORBIDDEN_MARKERS
                    if profile == "external_candidate"
                    else ()
                )
                findings.extend(_file_marker_hits(path, rel, markers))
                if not allowed:
                    findings.append({"kind": "unknown_executable_member", "value": rel})
                if script and rel != main_rel and not allowed:
                    findings.append({"kind": "executable_script", "value": rel})

    records.sort(key=lambda item: item["path"])
    present = {item["path"] for item in records if item["allowed"]}
    for rel, contract in sorted(approved_executables.items()):
        if rel in present:
            continue
        findings.append(
            {
                "kind": (
                    "main_executable_missing"
                    if contract == "main_executable"
                    else "approved_executable_missing"
                ),
                "value": rel,
            }
        )
    return records, findings, _canonical_digest(records)


def scan_collected_members(profile: str, modules: list[tuple[str, str]], resources: list[str]) -> dict:
    if profile not in PROFILES:
        raise ValueError("未知 build profile")
    module_names = sorted({str(name) for name, _path in modules})
    resource_names = sorted({_normal_path(name) for name in resources})
    findings: list[dict[str, str]] = []

    for name, source in modules:
        name = str(name)
        if _forbidden_module(profile, name):
            findings.append({"kind": "module", "value": name})
        path = Path(source)
        if profile == "external_candidate" and path.is_file() and path.suffix == ".py":
            markers = _project_source_markers(name, path)
            if markers:
                findings.extend(_marker_hits(name, path.read_bytes(), markers))
    for resource in resource_names:
        lowered = resource.lower()
        if profile not in {"trusted_test", "trusted_test_safe_e2e"} and Path(resource).name == "release_features.json":
            findings.append({"kind": "resource", "value": resource})
        if profile == "external_candidate":
            if Path(resource).name == "license_runtime_config.json":
                findings.append({"kind": "resource", "value": resource})
            module_name = _resource_module_name(resource)
            if module_name and _forbidden_module(profile, module_name):
                findings.append({"kind": "resource", "value": resource})
            if any(marker in lowered for marker in ("selftest", "selfcheck", "internal-test", "internal_test", "run_internal", "mock_transport")):
                findings.append({"kind": "resource", "value": resource})
        elif profile == "trusted_test" and _trusted_forbidden_resource(resource):
            findings.append({"kind": "resource", "value": resource})
        elif profile == "trusted_test_safe_e2e" and _safe_e2e_forbidden_resource(resource):
            findings.append({"kind": "resource", "value": resource})
        elif profile == "internal_test" and Path(resource).name in _INTERNAL_FORBIDDEN_RESOURCES:
            findings.append({"kind": "resource", "value": resource})

    return {
        "profile": profile,
        "module_count": len(module_names),
        "resource_count": len(resource_names),
        "forbidden_count": len(findings),
        "findings": findings,
    }


def write_analysis_receipt(path: Path, profile: str, modules, resources, profile_sha256: str) -> dict:
    path = Path(path)
    normalized_modules = [(str(item[0]), str(item[1])) for item in modules if len(item) >= 2]
    normalized_resources = [str(item[0] if isinstance(item, (tuple, list)) else item) for item in resources]
    scan = scan_collected_members(profile, normalized_modules, normalized_resources)
    module_inventory = sorted({name for name, _source in normalized_modules})
    resource_inventory = sorted({_normal_path(name) for name in normalized_resources})
    inventory = {
        "modules": module_inventory,
        "resources": resource_inventory,
        "profile_sha256": profile_sha256,
    }
    result = {
        "schema": 2,
        **scan,
        "profile_sha256": profile_sha256,
        "modules": module_inventory,
        "resources": resource_inventory,
        "inventory_digest": _canonical_digest(inventory),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if result["forbidden_count"]:
        raise SystemExit("构建成员扫描发现禁止项")
    return result


def _walk_inventory(root: Path) -> tuple[list[str], list[dict], str]:
    names: list[str] = []
    records: list[dict] = []

    def visit(current: Path) -> None:
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            st = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                names.append(rel)
                records.append({"path": rel, "type": "symlink", "mode": mode, "target": target})
            elif stat.S_ISDIR(st.st_mode):
                records.append({"path": rel, "type": "dir", "mode": mode})
                visit(path)
            elif stat.S_ISREG(st.st_mode):
                names.append(rel)
                records.append({"path": rel, "type": "file", "mode": mode, "size": st.st_size, "sha256": _sha256(path)})
            else:
                raise SystemExit(f"App 实物含特殊文件:{rel}")

    visit(root)
    return sorted(names), records, _canonical_digest(records)


def _project_resource_names(
    resource_names: set[str],
    final_records: list[dict],
    materialized_alias_roots: tuple[str, ...] = (),
) -> set[str]:
    alias_roots = {
        root.removeprefix("Contents/Resources/")
        for root in materialized_alias_roots
        if root.startswith("Contents/Resources/")
    }
    alias_roots.update(
        record["path"]
        for record in final_records
        if record.get("type") == "symlink"
    )
    symlink_roots = sorted(
        alias_roots,
        key=lambda value: (value.count("/"), value),
    )
    projected: set[str] = set()
    for name in resource_names:
        collapsed = name
        for root in symlink_roots:
            if name == root or name.startswith(root + "/"):
                collapsed = root
                break
        projected.add(collapsed)
    return projected


def _materialized_context_for_app(app: Path, profile: str) -> dict[str, object] | None:
    if profile != "external_candidate" or (app / "Contents/Resources/component_delivery.v2.json").exists():
        return None
    root = app.parent
    materialization = root / "agent-symlink-materialization-receipt.json"
    signed_stage = root / "signed-stage-build-profile-receipt.json"
    present = (materialization.exists(), signed_stage.exists())
    if present == (False, False):
        return None
    if present != (True, True):
        raise RuntimeError("MATERIALIZED_PROFILE_RECEIPT_PAIR_INCOMPLETE")
    return _MATERIALIZATION_GATE["validate_materialized_projection"](
        app,
        materialization,
        signed_stage,
    )


def candidate_app_tree_identity(app: Path, profile: str) -> dict[str, object]:
    """Return a final identity or an explicit external signed-stage placeholder."""

    if (app / "Contents/Resources/component_delivery.v2.json").exists():
        from src.core.agent_entry.delivery_identity import delivery_app_tree_identity, verify_delivery_marker
        verify_delivery_marker(app)
        return delivery_app_tree_identity(app)
    if canonical_app_tree_identity is None:
        raise RuntimeError("canonical App tree identity module unavailable")
    try:
        return canonical_app_tree_identity(app.resolve())
    except CanonicalAppTreeError as exc:
        if profile != "external_candidate" or str(exc) != "APP_TREE_SYMLINK_FORBIDDEN":
            raise
        return {
            "schema_id": "lulu.canonical-app-tree/v1",
            "state": "pending_materialization",
            "sha256": None,
            "reason": "APP_TREE_SYMLINK_FORBIDDEN",
        }


def _resource_findings(profile: str, resources_root: Path, names: list[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for name in names:
        lowered = name.lower()
        path = resources_root / name
        if profile not in {"trusted_test", "trusted_test_safe_e2e"} and Path(name).name == "release_features.json":
            findings.append({"kind": "resource", "value": name})
        if profile == "external_candidate":
            if Path(name).name == "license_runtime_config.json":
                findings.append({"kind": "resource", "value": name})
            if any(marker in lowered for marker in ("selftest", "selfcheck", "internal-test", "internal_test", "run_internal", "mock_transport")):
                findings.append({"kind": "resource", "value": name})
            module_name = _resource_module_name(name)
            if module_name and _forbidden_module(profile, module_name):
                findings.append({"kind": "resource", "value": name})
            markers = _external_module_markers(module_name or "")
            try:
                st = path.lstat()
            except FileNotFoundError:
                raise SystemExit("Resources inventory 在扫描中发生变化")
            if markers and stat.S_ISREG(st.st_mode):
                with path.open("rb") as handle:
                    overlap = b""
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        data = overlap + block
                        findings.extend(_marker_hits(name, data, markers))
                        overlap = data[-128:]
        elif profile == "trusted_test" and _trusted_forbidden_resource(name):
            findings.append({"kind": "resource", "value": name})
        elif profile == "trusted_test_safe_e2e" and _safe_e2e_forbidden_resource(name):
            findings.append({"kind": "resource", "value": name})
        elif profile == "internal_test" and Path(name).name in _INTERNAL_FORBIDDEN_RESOURCES:
            findings.append({"kind": "resource", "value": name})
    return findings


def _archive_inventory(executable: Path, profile: str) -> tuple[list[str], list[dict[str, str]], dict]:
    try:
        from PyInstaller.archive.readers import ArchiveReadError, CArchiveReader
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("缺少项目既有 PyInstaller archive reader") from exc

    findings: list[dict[str, str]] = []
    try:
        archive = CArchiveReader(str(executable))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("最终 executable 不是可解析的 PyInstaller CArchive") from exc
    if not archive.toc:
        raise SystemExit("最终 executable 的 PyInstaller CArchive 为空")

    modules: set[str] = set()
    product_code_sha256: dict[str, str] = {}
    product_entry_scripts: set[str] = set()
    runtime_hook_scripts: set[str] = set()
    pyz_count = 0
    pyz_archive_count = 0
    for name, entry in archive.toc.items():
        typecode = entry[-1]
        if typecode in {"m", "s"}:
            modules.add(name)
            if typecode == "s":
                if name in _FIXED_RUNTIME_HOOK_SCRIPTS:
                    runtime_hook_scripts.add(name)
                elif name.startswith("run_") or not (
                    name.startswith("pyi_rth_")
                    or name.startswith("pyiboot")
                    or name.startswith("pyimod")
                    or name.startswith("rthook_")
                ):
                    product_entry_scripts.add(name)
            if name.startswith("run_"):
                try:
                    product_code_sha256[name] = _stable_code_sha256(
                        marshal.loads(archive.extract(name))
                    )
                except Exception as exc:  # noqa: BLE001
                    raise SystemExit(f"最终 executable 的产品入口无法解析:{name}") from exc
            if _forbidden_module(profile, name):
                findings.append({"kind": "archive_module", "value": name})
            if profile == "external_candidate":
                markers = _external_module_markers(name)
                if markers:
                    findings.extend(_marker_hits(name, archive.extract(name), markers))
        elif typecode == "z":
            try:
                pyz = archive.open_embedded_archive(name)
            except Exception as exc:  # noqa: BLE001
                raise SystemExit("最终 executable 的嵌入 PYZ 无法解析") from exc
            if not pyz.toc:
                raise SystemExit("最终 executable 的嵌入 PYZ 为空")
            pyz_archive_count += 1
            pyz_count += len(pyz.toc)
            for module_name in pyz.toc:
                modules.add(module_name)
                if module_name == "src" or module_name.startswith("src."):
                    try:
                        raw_code = pyz.extract(module_name, raw=True)
                        if not raw_code:
                            raise ValueError("empty code")
                        product_code_sha256[module_name] = _stable_code_sha256(
                            marshal.loads(raw_code)
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise SystemExit(
                            f"最终 executable 的Lulu产品模块无法解析:{module_name}"
                        ) from exc
                if _forbidden_module(profile, module_name):
                    findings.append({"kind": "pyz_module", "value": module_name})
                if profile == "external_candidate":
                    markers = _external_module_markers(module_name)
                    if markers:
                        raw = pyz.extract(module_name, raw=True)
                        if raw:
                            findings.extend(_marker_hits(module_name, raw, markers))
    if not modules or pyz_archive_count == 0 or pyz_count == 0:
        raise SystemExit("最终 executable 缺少非空 CArchive/PYZ 模块结构")
    return sorted(modules), findings, {
        "format": "pyinstaller_carchive",
        "carchive_count": len(archive.toc),
        "pyz_archive_count": pyz_archive_count,
        "pyz_count": pyz_count,
        "executable_sha256": _sha256(executable),
        "app_entry_scripts": sorted(product_entry_scripts),
        "product_entry_scripts": sorted(product_entry_scripts),
        "runtime_hook_scripts": sorted(runtime_hook_scripts),
        "product_code_sha256": dict(sorted(product_code_sha256.items())),
    }


def _approved_entry_contracts(
    app: Path,
    main_executable: Path,
    profile: str,
    expected_modules: set[str],
) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = [
        {
            "module": Path(PROFILES[profile]["entry"]).stem,
            "relative_path": main_executable.relative_to(app).as_posix(),
            "contract": "main_executable",
            "path": main_executable,
        }
    ]
    candidates = tuple(
        value
        for value in (
            APPROVED_SECONDARY_ENTRY_CONTRACTS.get(profile),
            APPROVED_TERTIARY_ENTRY_CONTRACTS.get(profile),
        )
        if value is not None
    )
    for secondary in candidates:
        if secondary["module"] in expected_modules:
            contracts.append(
                {
                    **secondary,
                    "path": app / secondary["relative_path"],
                }
            )
    return contracts


def _combined_archive_inventory(
    entry_contracts: list[dict[str, object]],
    profile: str,
    archive_reader,
) -> tuple[list[str], list[dict[str, str]], dict]:
    modules: set[str] = set()
    findings: list[dict[str, str]] = []
    product_entry_scripts: set[str] = set()
    runtime_hook_scripts: set[str] = set()
    product_code_sha256: dict[str, str] = {}
    executable_records: list[dict[str, object]] = []
    shared_modules: set[str] | None = None
    carchive_count = 0
    pyz_archive_count = 0
    pyz_count = 0

    for entry in entry_contracts:
        executable = Path(entry["path"])
        relative_path = str(entry["relative_path"])
        expected_entry = str(entry["module"])
        archive_modules, archive_findings, archive_meta = archive_reader(
            executable,
            profile,
        )
        normalized_modules = set(map(str, archive_modules))
        normalized_entries = sorted(
            set(
                map(
                    str,
                    archive_meta.get(
                        "product_entry_scripts",
                        archive_meta.get("app_entry_scripts") or [],
                    ),
                )
            )
        )
        raw_runtime_hooks = archive_meta.get("runtime_hook_scripts")
        normalized_runtime_hooks = sorted(
            set(map(str, raw_runtime_hooks or []))
        )
        if normalized_entries != [expected_entry]:
            findings.append(
                {
                    "kind": "archive_entry_contract",
                    "value": (
                        f"{relative_path}:expected={expected_entry}:"
                        f"actual={','.join(normalized_entries)}"
                    ),
                }
            )
        expected_runtime_hooks = sorted(_PROFILE_RUNTIME_HOOK_SCRIPTS[profile])
        if (
            type(raw_runtime_hooks) is not list
            or normalized_runtime_hooks != expected_runtime_hooks
        ):
            findings.append(
                {
                    "kind": "archive_runtime_hook_contract",
                    "value": (
                        f"{relative_path}:expected="
                        f"{','.join(expected_runtime_hooks)}:"
                        f"actual={','.join(normalized_runtime_hooks)}"
                    ),
                }
            )
        current_shared = normalized_modules - set(normalized_entries)
        if shared_modules is None:
            shared_modules = current_shared
        elif current_shared != shared_modules:
            findings.append(
                {
                    "kind": "archive_shared_inventory_mismatch",
                    "value": relative_path,
                }
            )

        current_product_code = archive_meta.get("product_code_sha256") or {}
        for name, digest in current_product_code.items():
            existing = product_code_sha256.get(str(name))
            if existing is not None and existing != digest:
                findings.append(
                    {
                        "kind": "archive_product_code_mismatch",
                        "value": f"{relative_path}:{name}",
                    }
                )
            else:
                product_code_sha256[str(name)] = str(digest)

        modules.update(normalized_modules)
        findings.extend(archive_findings)
        product_entry_scripts.update(normalized_entries)
        runtime_hook_scripts.update(normalized_runtime_hooks)
        carchive_count += int(archive_meta.get("carchive_count") or 0)
        pyz_archive_count += int(archive_meta.get("pyz_archive_count") or 0)
        pyz_count = max(pyz_count, int(archive_meta.get("pyz_count") or 0))
        executable_records.append(
            {
                "path": relative_path,
                "contract": str(entry["contract"]),
                "entry_module": expected_entry,
                "sha256": str(archive_meta.get("executable_sha256") or ""),
                "carchive_count": int(archive_meta.get("carchive_count") or 0),
                "pyz_archive_count": int(
                    archive_meta.get("pyz_archive_count") or 0
                ),
                "pyz_count": int(archive_meta.get("pyz_count") or 0),
            }
        )

    primary_sha256 = (
        str(executable_records[0]["sha256"]) if executable_records else ""
    )
    shared_inventory = sorted(shared_modules or set())
    return sorted(modules), findings, {
        "format": "pyinstaller_carchive",
        "carchive_count": carchive_count,
        "pyz_archive_count": pyz_archive_count,
        "pyz_count": pyz_count,
        "executable_sha256": primary_sha256,
        "app_entry_scripts": sorted(product_entry_scripts),
        "product_entry_scripts": sorted(product_entry_scripts),
        "runtime_hook_scripts": sorted(runtime_hook_scripts),
        "product_code_sha256": dict(sorted(product_code_sha256.items())),
        "shared_module_count": len(shared_inventory),
        "shared_modules_sha256": _canonical_digest(shared_inventory),
        "executables": executable_records,
    }


def _valid_external_runtime_config(path: Path) -> dict:
    """The official free build must not ship even a dangling license config."""
    try:
        path.lstat()
    except FileNotFoundError:
        return {"legacy_license_config_absent": True}
    except OSError:
        return {"legacy_license_config_absent": False}
    return {"legacy_license_config_absent": False}


def _valid_release_features(path: Path, profile: str) -> dict:
    checks = {"present": False, "regular": False, "valid": False, "profile": False}
    try:
        st = path.lstat()
        checks["present"] = True
        checks["regular"] = stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)
        if checks["regular"]:
            payload = _RELEASE_FEATURES["load_manifest"](
                path,
                expected_profile=profile,
            )
            checks["valid"] = True
            checks["profile"] = payload.get("profile") == profile
    except Exception:  # noqa: BLE001
        return checks
    return checks


def _load_analysis(path: Path, profile: str, profile_sha: str) -> tuple[dict, str]:
    try:
        analysis = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("Analysis 收据缺失或损坏") from exc
    required = {"schema", "profile", "profile_sha256", "modules", "resources", "inventory_digest", "forbidden_count"}
    if not isinstance(analysis, dict) or not required.issubset(analysis):
        raise SystemExit("Analysis 收据格式陈旧或不完整")
    if analysis.get("schema") != 2 or analysis.get("profile") != profile or analysis.get("profile_sha256") != profile_sha:
        raise SystemExit("Analysis 收据 profile 或 profile SHA 不匹配")
    modules = analysis.get("modules")
    resources = analysis.get("resources")
    if not isinstance(modules, list) or not isinstance(resources, list) or modules != sorted(set(modules)) or resources != sorted(set(resources)):
        raise SystemExit("Analysis inventory 非规范集合")
    expected_digest = _canonical_digest({"modules": modules, "resources": resources, "profile_sha256": profile_sha})
    if analysis.get("inventory_digest") != expected_digest:
        raise SystemExit("Analysis inventory digest 不匹配")
    return analysis, _sha256(path)


def _read_regular_json(path: Path, label: str) -> dict:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise SystemExit(f"{label}缺失或不可读取") from exc
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise SystemExit(f"{label}必须是普通文件")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"{label}损坏") from exc
    if type(payload) is not dict:
        raise SystemExit(f"{label}格式无效")
    return payload


def _product_source_checks(
    payload: dict,
    profile: str,
    archive_meta: dict,
    resources_root: Path,
    *,
    expected_manifest: dict | None,
    expected_entry_scripts: list[str] | None = None,
) -> tuple[dict[str, bool], dict]:
    expected = PROFILES[profile]
    entry_module = Path(expected["entry"]).stem
    required_modules = list(expected["required_modules"])
    module_records = payload.get("modules")
    resource_records = payload.get("resources")
    product_code = archive_meta.get("product_code_sha256")
    entry_scripts = archive_meta.get(
        "product_entry_scripts",
        archive_meta.get("app_entry_scripts"),
    )
    shape_valid = (
        type(module_records) is dict
        and type(resource_records) is dict
        and type(product_code) is dict
        and type(entry_scripts) is list
        and payload.get("schema") == 1
        and payload.get("profile") == profile
        and payload.get("entry_module") == entry_module
        and payload.get("required_modules") == required_modules
        and set(module_records or {}) == {entry_module, *required_modules}
        and set(resource_records or {}) == set(_PRODUCT_SOURCE_RESOURCES)
        and type(payload.get("spec")) is dict
    )
    archive_matches = bool(shape_valid)
    resources_match = bool(shape_valid)
    if shape_valid:
        for module, record in module_records.items():
            if (
                type(record) is not dict
                or set(record) != {"path", "sha256", "code_sha256"}
                or not _SHA256_RE.fullmatch(str(record.get("sha256")))
                or not _SHA256_RE.fullmatch(str(record.get("code_sha256")))
                or product_code.get(module) != record.get("code_sha256")
            ):
                archive_matches = False
                break
        for bundle_path, record in resource_records.items():
            path = resources_root / bundle_path
            try:
                mode = path.lstat().st_mode
                valid_file = stat.S_ISREG(mode) and not path.is_symlink()
            except OSError:
                valid_file = False
            if (
                type(record) is not dict
                or set(record) != {"path", "sha256"}
                or not _SHA256_RE.fullmatch(str(record.get("sha256")))
                or not valid_file
                or _sha256(path) != record.get("sha256")
            ):
                resources_match = False
                break
    expected_matches = expected_manifest is None or payload == expected_manifest
    expected_entries = (
        [entry_module]
        if expected_entry_scripts is None
        else sorted(set(expected_entry_scripts))
    )
    checks = {
        "product_source_shape_valid": bool(shape_valid),
        "product_entry_exact": entry_scripts == expected_entries,
        "required_product_modules_present": all(
            module in set(archive_meta.get("product_code_sha256") or {})
            for module in required_modules
        ),
        "product_code_matches_source": archive_matches,
        "product_resources_match_source": resources_match,
        "product_source_matches_fixed_tree": expected_matches,
    }
    return checks, {
        "sha256": product_source_sha256(payload) if shape_valid else None,
        "entry_module": entry_module,
        "required_modules": required_modules,
        "app_entry_scripts": entry_scripts if type(entry_scripts) is list else [],
        "product_entry_scripts": entry_scripts if type(entry_scripts) is list else [],
        "runtime_hook_scripts": (
            archive_meta.get("runtime_hook_scripts")
            if type(archive_meta.get("runtime_hook_scripts")) is list
            else []
        ),
    }


def _verify_app_with_reader(
    app: Path,
    profile: str,
    analysis_receipt: Path,
    output: Path,
    archive_reader,
    *,
    expected_source_identity: tuple[str, str] | None = None,
    expected_product_source: dict | None = None,
    macos_compatibility: dict | None = None,
    materialized_context: dict[str, object] | None = None,
) -> dict:
    expected = PROFILES.get(profile)
    if expected is None:
        raise SystemExit("未知 build profile")
    app_st = app.lstat()
    if stat.S_ISLNK(app_st.st_mode) or not stat.S_ISDIR(app_st.st_mode):
        raise SystemExit("App 必须是非符号链接目录")
    contents = app / "Contents"
    resources = contents / "Resources"
    profile_path = resources / "build_profile.json"
    authorship_path = resources / "AUTHORSHIP.json"
    product_source_path = resources / _PRODUCT_SOURCE_RESOURCE
    info_path = contents / "Info.plist"
    payload = _read_regular_json(profile_path, "build_profile.json")
    profile_sha = _sha256(profile_path)
    analysis, analysis_sha = _load_analysis(analysis_receipt, profile, profile_sha)
    authorship = _read_regular_json(authorship_path, "AUTHORSHIP.json")
    product_source = _read_regular_json(product_source_path, _PRODUCT_SOURCE_RESOURCE)
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable_name = str(info.get("CFBundleExecutable") or expected["artifact_prefix"])
    executable = contents / "MacOS" / executable_name
    if materialized_context is None:
        materialized_context = _materialized_context_for_app(app, profile)
    materialized_alias_roots = tuple(
        str(item) for item in (materialized_context or {}).get("alias_roots", [])
    )

    resource_names, resource_records, resource_tree_digest = _walk_inventory(resources)
    expected_modules = set(analysis["modules"])
    entry_contracts = _approved_entry_contracts(
        app,
        executable,
        profile,
        expected_modules,
    )
    module_names, archive_findings, archive_meta = _combined_archive_inventory(
        entry_contracts,
        profile,
        archive_reader,
    )
    product_checks, product_summary = _product_source_checks(
        product_source,
        profile,
        archive_meta,
        resources,
        expected_manifest=expected_product_source,
        expected_entry_scripts=[str(entry["module"]) for entry in entry_contracts],
    )
    approved_executables = {
        str(entry["relative_path"]): str(entry["contract"])
        for entry in entry_contracts
    }
    executable_records, executable_findings, executable_inventory_digest = _bundle_executable_inventory(
        app,
        approved_executables,
        profile,
        set(analysis["resources"]),
        materialized_alias_roots,
    )
    privacy_findings = _PRIVACY_GATE["scan_bundle_privacy"](
        app,
        forbidden_markers=(str(Path.home()), str(_APP_ROOT.resolve())),
    )
    final_findings = (
        _resource_findings(profile, resources, resource_names)
        + archive_findings
        + executable_findings
        + privacy_findings
    )
    actual_modules = set(module_names) - _PYINSTALLER_BOOTSTRAP_MODULES
    expected_resources = _project_resource_names(
        set(analysis["resources"]),
        resource_records,
        materialized_alias_roots,
    )
    v2_delivery = (app / "Contents/Resources/component_delivery.v2.json").exists()
    if v2_delivery:
        expected_resources.add("agent_links.v2.json")
        if (app / "Contents/Resources/standalone-install.v1.json").exists():
            from src.core.agent_entry.standalone_source import inspect_install_source
            # This post-analysis resource is accepted only after its sealed
            # identity and the entire signed App have passed the same installer
            # verifier. Unknown files still fail the closed inventory below.
            inspect_install_source(app)
            expected_resources.add("standalone-install.v1.json")
    actual_resources = _project_resource_names(
        set(resource_names),
        resource_records,
        materialized_alias_roots,
    )
    differences = {
        "modules_missing": sorted(expected_modules - actual_modules),
        "modules_extra": sorted(actual_modules - expected_modules),
        "resources_missing": sorted(expected_resources - actual_resources),
        "resources_extra": sorted(actual_resources - expected_resources),
    }
    difference_count = sum(len(items) for items in differences.values())
    analysis_projection_digest = _canonical_digest(
        {
            "modules": sorted(expected_modules),
            "resources": sorted(expected_resources),
            "profile_sha256": profile_sha,
        }
    )
    final_projection_digest = _canonical_digest(
        {
            "modules": sorted(actual_modules),
            "resources": sorted(actual_resources),
            "profile_sha256": profile_sha,
        }
    )
    final_inventory_digest = _canonical_digest(
        {
            "modules": module_names,
            "resources": resource_names,
            "executables": executable_records,
            "profile_sha256": profile_sha,
        }
    )
    code_resources = contents / "_CodeSignature" / "CodeResources"
    seal = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    direct_bundle_seal_valid = seal.returncode == 0
    materialized_bundle_seal_valid = bool(
        materialized_context
        and materialized_context.get("valid") is True
        and materialized_context.get("signed_stage_bundle_seal_valid") is True
    )
    strict_seal = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], capture_output=True, text=True) if v2_delivery else None
    bundle_seal_valid = (direct_bundle_seal_valid and strict_seal.returncode == 0) if v2_delivery else (direct_bundle_seal_valid or materialized_bundle_seal_valid)
    runtime_checks = _valid_external_runtime_config(resources / "license_runtime_config.json") if profile == "external_candidate" else {"not_applicable": True}
    release_feature_checks = (
        _valid_release_features(resources / "release_features.json", profile)
        if profile == "trusted_test"
        else {"not_applicable": True}
    )
    macos_compatibility = macos_compatibility or {
        "ok": True,
        "not_evaluated": True,
    }
    tree_names, _tree_records, _legacy_app_tree_digest = _walk_inventory(app)
    app_tree_identity = candidate_app_tree_identity(app, profile)
    app_tree_digest = app_tree_identity["sha256"]
    source_commit = payload.get("source_commit")
    source_tree = payload.get("source_tree")
    try:
        expected_profile_payload = profile_payload(
            profile,
            version=str(payload.get("version")),
            build=str(payload.get("build")),
            source_commit=str(source_commit),
            source_tree=str(source_tree),
            product_source_sha256=str(payload.get("product_source_sha256")),
        )
        source_identity_valid = True
    except ValueError:
        expected_profile_payload = None
        source_identity_valid = False
    if expected_source_identity is None:
        source_matches_build_input = source_identity_valid
    else:
        source_matches_build_input = (
            source_identity_valid
            and (source_commit, source_tree) == expected_source_identity
        )
    checks = {
        "profile_exact": payload == expected_profile_payload,
        "source_identity_valid": source_identity_valid,
        "source_matches_build_input": source_matches_build_input,
        "authorship_profile": authorship.get("build_profile") == profile,
        "authorship_profile_sha256": authorship.get("build_profile_sha256") == profile_sha,
        "authorship_source_commit": authorship.get("source_commit") == source_commit,
        "authorship_source_tree": authorship.get("source_tree") == source_tree,
        "authorship_product_source_sha256": (
            authorship.get("product_source_sha256")
            == payload.get("product_source_sha256")
            == product_summary["sha256"]
        ),
        "bundle_id": info.get("CFBundleIdentifier") == expected["bundle_id"],
        "bundle_name": info.get("CFBundleName") == expected["app_name"],
        "artifact_name": app.name == f"{expected['artifact_prefix']}.app",
        "gui_foreground_contract": (
            profile != "trusted_test" or info.get("LSBackgroundOnly") in (None, False)
        ),
        "analysis_forbidden_zero": analysis.get("forbidden_count") == 0,
        "final_forbidden_zero": len(final_findings) == 0,
        "bundle_executable_contract_valid": all(item["allowed"] for item in executable_records),
        "inventory_difference_zero": difference_count == 0,
        "inventory_digest_matches": analysis_projection_digest == final_projection_digest,
        "code_resources_present": code_resources.is_file() and not code_resources.is_symlink(),
        "bundle_seal_valid": bundle_seal_valid,
        "archive_structure_valid": (
            archive_meta.get("format") == "pyinstaller_carchive"
            and int(archive_meta.get("carchive_count") or 0) > 0
            and int(archive_meta.get("pyz_archive_count") or 0) > 0
            and int(archive_meta.get("pyz_count") or 0) > 0
        ),
        "runtime_config_valid": all(runtime_checks.values()),
        "release_features_valid": all(release_feature_checks.values()),
        "release_privacy_valid": len(privacy_findings) == 0,
        "macos_15_compatibility_valid": bool(macos_compatibility.get("ok")),
        **product_checks,
    }
    result = {
        "schema": 2,
        "profile": profile,
        "package_surface": expected.get("package_surface"),
        "ok": all(checks.values()),
        "profile_in_bundle_seal": checks["bundle_seal_valid"] and checks["code_resources_present"],
        "checks": checks,
        "digests": {
            "final_inventory_sha256": final_inventory_digest,
            "analysis_scope_inventory_sha256": final_projection_digest,
            "analysis_inventory_sha256": analysis.get("inventory_digest"),
            "analysis_projection_sha256": analysis_projection_digest,
            "bundle_executable_inventory_sha256": executable_inventory_digest,
            "analysis_file_sha256": analysis_sha,
            "app_tree_sha256": app_tree_digest,
            "resources_tree_sha256": resource_tree_digest,
            "code_resources_sha256": _sha256(code_resources) if code_resources.is_file() else None,
            "profile_sha256": profile_sha,
            "executable_sha256": archive_meta["executable_sha256"],
            "product_source_sha256": product_summary["sha256"],
        },
        "app_tree": app_tree_identity,
        "app_tree_binding": (
            {
                "stage": "post_materialization",
                "materialization_receipt_sha256": materialized_context[
                    "materialization_receipt_sha256"
                ],
                "materialized_count": materialized_context["materialized_count"],
                "first_recomputation": app_tree_identity["sha256"],
                "second_recomputation": materialized_context["app_tree"]["sha256"],
            }
            if materialized_context
            else None
        ),
        "bundle_seal": {
            "direct_codesign_returncode": seal.returncode,
            "direct_codesign_stderr": str(seal.stderr or "").strip(),
            "verification": (
                "direct_codesign"
                if direct_bundle_seal_valid
                else "verified_materialization_of_signed_stage"
                if materialized_bundle_seal_valid
                else "invalid"
            ),
            "signed_stage_profile_receipt_sha256": (
                materialized_context.get("signed_stage_profile_receipt_sha256")
                if materialized_context
                else None
            ),
        },
        "inventory": {
            "module_count": len(module_names),
            "resource_count": len(resource_names),
            "app_entry_count": len(tree_names),
            "bundle_executable_count": len(executable_records),
            "bundle_executables": executable_records,
            "archive": archive_meta,
            "difference_count": difference_count,
            "differences": differences,
        },
        "final_findings": final_findings,
        "runtime_config": runtime_checks,
        "release_features": release_feature_checks,
        "macos_compatibility": macos_compatibility,
        "privacy": {
            "finding_count": len(privacy_findings),
            "findings": privacy_findings,
        },
        "upload_target": expected["upload_target"],
        "source_identity": {
            "source_commit": source_commit if source_identity_valid else None,
            "source_tree": source_tree if source_identity_valid else None,
            "build_input_source_commit": (
                expected_source_identity[0]
                if expected_source_identity is not None
                else source_commit if source_identity_valid else None
            ),
            "build_input_source_tree": (
                expected_source_identity[1]
                if expected_source_identity is not None
                else source_tree if source_identity_valid else None
            ),
        },
        "product_source": product_summary,
    }
    if v2_delivery:
        second = candidate_app_tree_identity(app, profile)
        if second != app_tree_identity:
            raise RuntimeError("FINAL_APP_TREE_DRIFT")
        result["app_tree_binding"] = {"stage":"final_signed_v2",
            "first_recomputation":app_tree_identity["sha256"],"second_recomputation":second["sha256"]}
        result["bundle_seal"]["strict_returncode"] = strict_seal.returncode
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if not result["ok"]:
        raise SystemExit("最终 App 实物扫描或收据绑定失败")
    return result


def verify_app(app: Path, profile: str, analysis_receipt: Path, output: Path) -> dict:
    """Production/CLI gate. Archive reader is fixed and cannot be overridden."""
    expected_source_identity = source_identity(
        allowed_dirty_paths=_BUILD_GENERATED_PATHS,
    )
    expected_product_source = product_source_manifest_from_git(
        profile,
        expected_source_identity[0],
        expected_source_identity[1],
    )
    materialized_context = _materialized_context_for_app(app, profile)
    if materialized_context:
        macos_compatibility = _MACOS_COMPATIBILITY_GATE["evaluate_app"](
            app,
            materialized_alias_roots=tuple(materialized_context["alias_roots"]),
        )
    else:
        macos_compatibility = _MACOS_COMPATIBILITY_GATE["evaluate_app"](app)
    return _verify_app_with_reader(
        app,
        profile,
        analysis_receipt,
        output,
        _archive_inventory,
        expected_source_identity=expected_source_identity,
        expected_product_source=expected_product_source,
        macos_compatibility=macos_compatibility,
        materialized_context=materialized_context,
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def rebind_final_app_tree(
    *,
    app: Path,
    profile_receipt_path: Path,
    materialization_receipt_path: Path,
) -> dict[str, object]:
    """Bind the signed-stage profile receipt to the final materialized tree."""

    app = app.resolve(strict=True)
    profile_path = profile_receipt_path.resolve(strict=True)
    materialization_path = materialization_receipt_path.resolve(strict=True)
    if app in profile_path.parents or app in materialization_path.parents:
        raise RuntimeError("FINAL_APP_TREE_RECEIPT_INSIDE_APP")
    profile_receipt = _read_regular_json(profile_path, "profile receipt")
    materialization = _read_regular_json(
        materialization_path, "materialization receipt"
    )
    records = materialization.get("records")
    if (
        profile_receipt.get("schema") != 2
        or profile_receipt.get("profile") != "external_candidate"
        or profile_receipt.get("ok") is not True
    ):
        raise RuntimeError("PROFILE_RECEIPT_NOT_READY")
    if (
        materialization.get("schema_id")
        != "lulu.agent-symlink-materialization/v1"
        or materialization.get("app_bundle") != app.name
        or materialization.get("remaining_symlink_count") != 0
        or type(records) is not list
        or materialization.get("materialized_count") != len(records)
    ):
        raise RuntimeError("MATERIALIZATION_RECEIPT_INVALID")
    first = canonical_app_tree_identity(app) if canonical_app_tree_identity else None
    second = canonical_app_tree_identity(app) if canonical_app_tree_identity else None
    if first is None or first != second:
        raise RuntimeError("FINAL_APP_TREE_RECOMPUTATION_DRIFT")
    digests = profile_receipt.get("digests")
    if type(digests) is not dict:
        raise RuntimeError("PROFILE_RECEIPT_NOT_READY")
    digests["app_tree_sha256"] = first["sha256"]
    profile_receipt["app_tree"] = first
    profile_receipt["app_tree_binding"] = {
        "stage": "post_materialization",
        "materialization_receipt_sha256": _sha256(materialization_path),
        "materialized_count": len(records),
        "first_recomputation": first["sha256"],
        "second_recomputation": second["sha256"],
    }
    _atomic_json(profile_path, profile_receipt)
    return profile_receipt


def read_app_profile(app: Path) -> dict:
    path = app / "Contents" / "Resources" / "build_profile.json"
    payload = _read_regular_json(path, "build_profile.json")
    profile = payload.get("profile")
    expected = PROFILES.get(str(profile))
    try:
        expected_payload = profile_payload(
            str(profile),
            version=str(payload.get("version")),
            build=str(payload.get("build")),
            source_commit=str(payload.get("source_commit")),
            source_tree=str(payload.get("source_tree")),
            product_source_sha256=str(payload.get("product_source_sha256")),
        )
    except ValueError as exc:
        raise SystemExit("App 构建来源缺失或损坏") from exc
    if expected is None or payload != expected_payload:
        raise SystemExit("App 构建身份缺失、未知或损坏")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--profile", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--build", required=True)
    create.add_argument("--build-venv-root", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--product-source-output", required=True)
    create_link = sub.add_parser("create-managed-venv-link")
    create_link.add_argument("--build-venv-root", required=True)
    remove_link = sub.add_parser("remove-managed-venv-link")
    remove_link.add_argument("--build-venv-root", required=True)
    remove_link.add_argument("--link-token", required=True)
    verify = sub.add_parser("verify-app")
    verify.add_argument("--profile", required=True)
    verify.add_argument("--app", required=True)
    verify.add_argument("--analysis-receipt", required=True)
    verify.add_argument("--output", required=True)
    bind_tree = sub.add_parser("bind-final-app-tree")
    bind_tree.add_argument("--app", required=True)
    bind_tree.add_argument("--profile-receipt", required=True)
    bind_tree.add_argument("--materialization-receipt", required=True)
    args = parser.parse_args()
    if args.command == "create":
        # build_app.sh 会在生成来源封印前先创建本次构建的受管配置。
        # 这些精确路径由脚本退出 trap 清理，不属于产品源码漂移。
        managed_venv_path = managed_build_venv_dirty_path(args.build_venv_root)
        source_commit, source_tree = source_identity(
            allowed_dirty_paths=(
                _BUILD_GENERATED_PATHS | frozenset({managed_venv_path})
            ),
        )
        product_source = product_source_manifest_from_git(
            args.profile,
            source_commit,
            source_tree,
        )
        product_source_digest = write_product_source(
            Path(args.product_source_output),
            product_source,
        )
        write_profile(
            Path(args.output),
            args.profile,
            version=args.version,
            build=args.build,
            source_commit=source_commit,
            source_tree=source_tree,
            product_source_sha256=product_source_digest,
        )
    elif args.command == "create-managed-venv-link":
        print(create_managed_build_venv_link(args.build_venv_root))
    elif args.command == "remove-managed-venv-link":
        remove_managed_build_venv_link(
            args.build_venv_root,
            args.link_token,
        )
    elif args.command == "verify-app":
        verify_app(Path(args.app), args.profile, Path(args.analysis_receipt), Path(args.output))
    else:
        rebind_final_app_tree(
            app=Path(args.app),
            profile_receipt_path=Path(args.profile_receipt),
            materialization_receipt_path=Path(args.materialization_receipt),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
