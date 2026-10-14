"""Read-only IndexTTS2 local readiness checks.

This module must stay stdlib-only apart from Lulu helpers. It does not import
IndexTTS2, torch, torchaudio, or load model weights.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import runtime_packages as rp
from .errors import scrub_detail
from ..artifact_trust import HF_MODEL_REVISIONS, install_receipt_valid


ENGINE_ID = "local_indextts2"
RUNTIME_REQUIRED = (
    "python",
    "python/nltk_data",
    "vendor/index-tts/indextts/cli_v2.py",
)
MODEL_REQUIRED = (
    "config.yaml",
    "gpt.pth",
    "s2mel.pth",
    "feat1.pt",
    "feat2.pt",
    "bpe.model",
    "wav2vec2bert_stats.pt",
    "qwen0.6bemo4-merge/config.json",
    "qwen0.6bemo4-merge/model.safetensors",
    "hf_cache/campplus_cn_common.bin",
    "hf_cache/semantic_codec_model.safetensors",
    "hf_cache/bigvgan/config.json",
    "hf_cache/bigvgan/bigvgan_generator.pt",
    "hf_cache/w2v-bert-2.0/config.json",
    "hf_cache/w2v-bert-2.0/preprocessor_config.json",
    "hf_cache/w2v-bert-2.0/model.safetensors",
)
MODEL_25_REQUIRED = tuple(item for item in MODEL_REQUIRED if item not in {
    "bpe.model", "hf_cache/semantic_codec_model.safetensors",
}) + ("codec.pth", "multilingual_zh_ja_yue_char_del.tiktoken")


@dataclass(frozen=True)
class IndexTTS2RuntimeStatus:
    runtime_dir: Path
    model_dir: Path
    config_path: Path
    python_executable: Path
    cli_executable: Path
    vendor_dir: Path
    nltk_data_dir: Path
    runtime_ready: bool
    model_ready: bool
    config_ready: bool
    runtime_source: str = "managed"
    model_source: str = "managed"
    config_source: str = "derived"
    config_state: str = "missing"
    missing_items: tuple[str, ...] = ()
    runtime_size_bytes: int = 0
    model_size_bytes: int = 0
    detail_redacted: str = ""
    model_version: str = "2.0"

    @property
    def ready(self) -> bool:
        return self.runtime_ready and self.model_ready and self.config_ready

    @property
    def is_development(self) -> bool:
        return self.runtime_source != "managed" or self.model_source != "managed"

    def summary(self) -> str:
        state = "ready" if self.ready else "missing"
        source = "development" if self.is_development else "managed"
        missing = ", ".join(self.missing_items[:6])
        if len(self.missing_items) > 6:
            missing += f", +{len(self.missing_items) - 6}"
        return (
            f"IndexTTS2 status={state}; source={source}; "
            f"runtime_size={_format_bytes(self.runtime_size_bytes)}; "
            f"model_size={_format_bytes(self.model_size_bytes)}; "
            f"config_state={self.config_state}; "
            f"missing={missing or 'none'}"
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "runtime_ready": self.runtime_ready,
            "model_ready": self.model_ready,
            "config_ready": self.config_ready,
            "runtime_source": self.runtime_source,
            "model_source": self.model_source,
            "config_source": self.config_source,
            "config_state": self.config_state,
            "vendor_ready": self.vendor_dir.is_dir(),
            "nltk_data_ready": self.nltk_data_dir.is_dir(),
            "missing_items": list(self.missing_items[:12]),
            "detail_redacted": self.detail_redacted,
        }


def runtime_root() -> Path:
    env = os.environ.get("LULU_INDEXTTS2_RUNTIME_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return rp.runtime_dir(ENGINE_ID)


def model_dir() -> Path:
    env = os.environ.get("LULU_INDEXTTS2_MODEL_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return rp.weights_dir(ENGINE_ID)


def _python_executable(runtime_dir: Path) -> Path:
    if os.name == "nt":
        return runtime_dir / "python" / "python.exe"
    binp = runtime_dir / "python" / "bin"
    cand = sorted(binp.glob("python3.*")) if binp.is_dir() else []
    return cand[0] if cand else binp / "python3"


def _cli_executable(runtime_dir: Path) -> Path:
    return _python_executable(runtime_dir)


def _vendor_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "vendor" / "index-tts"


def _nltk_data_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "python" / "nltk_data"


def _official_config_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "IndexTTS" / "config.toml"
    if sys_platform_is_darwin():
        return Path.home() / "Library" / "Application Support" / "IndexTTS" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "indextts" / "config.toml"


def official_config_path() -> Path:
    """Return the user-level IndexTTS CLI config path without creating it."""
    return _official_config_path()


def sys_platform_is_darwin() -> bool:
    return sys.platform == "darwin"


def _read_official_model_dir(config_path: Path) -> Path | None:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("model_dir"):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            return Path(value).expanduser().resolve(strict=False)
    return None


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _with_model_dir(text: str, target: Path) -> str:
    line = f"model_dir = {_toml_string(str(target))}"
    lines = text.splitlines()
    replaced = False
    out: list[str] = []
    for raw in lines:
        if raw.strip().startswith("model_dir"):
            out.append(line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def sync_official_config(
    runtime_dir: Path | None = None,
    checkpoints_dir: Path | None = None,
    runtime_source: str | None = None,
    model_source: str | None = None,
) -> IndexTTS2RuntimeStatus:
    """Legacy no-op: PBS runtime passes --model-dir and no longer needs global config."""
    before = check_runtime(
        runtime_dir,
        checkpoints_dir,
        runtime_source,
        model_source,
        include_size=False,
    )
    if not before.runtime_ready:
        raise RuntimeError("IndexTTS2 runtime 尚未准备完成,请到 AI 大模型配置安装。")
    if not before.model_ready:
        raise RuntimeError("IndexTTS2 model 尚未准备完成,请到 AI 大模型配置安装。")

    return check_runtime(
        runtime_dir,
        checkpoints_dir,
        runtime_source,
        model_source,
        include_size=False,
    )


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for item in path.rglob("*"):
        try:
            if not item.is_file():
                continue
            st = item.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        except OSError:
            pass
    return total


def _format_bytes(size: int) -> str:
    if size <= 0:
        return "0B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _missing_under(base: Path, required: tuple[str, ...], prefix: str) -> list[str]:
    if not base.is_dir():
        return [prefix]
    missing: list[str] = []
    for rel in required:
        if not (base / rel).exists():
            missing.append(f"{prefix}/{rel}")
    return missing


def check_runtime(
    runtime_dir: Path | None = None,
    checkpoints_dir: Path | None = None,
    runtime_source: str | None = None,
    model_source: str | None = None,
    *,
    include_size: bool = False,
    expected_version: str | None = None,
) -> IndexTTS2RuntimeStatus:
    if expected_version not in (None, "2.0", "2.5"):
        raise ValueError("不支持的 IndexTTS 版本")
    if expected_version == "2.0":
        base = rp.tts_root() / "local_indextts2"
        runtime_dir = runtime_dir or base / "runtime"
        checkpoints_dir = checkpoints_dir or base / "weights"
    rt = runtime_dir or runtime_root()
    md = checkpoints_dir or model_dir()
    cfg = md / "config.yaml"
    try:
        version_match = re.search(r'^version:\s*[\"\']?(2\.5)\b', cfg.read_text(encoding="utf-8"), re.MULTILINE)
    except (OSError, UnicodeError):
        version_match = None
    model_version = "2.5" if version_match else "2.0"
    vendor = _vendor_dir(rt)
    nltk = _nltk_data_dir(rt)
    py = _python_executable(rt)
    cli = _cli_executable(rt)
    runtime_source = runtime_source or ("env" if os.environ.get("LULU_INDEXTTS2_RUNTIME_DIR") and runtime_dir is None else "managed")
    model_source = model_source or ("env" if os.environ.get("LULU_INDEXTTS2_MODEL_DIR") and checkpoints_dir is None else "managed")
    missing: list[str] = []
    if expected_version is not None and model_version != expected_version:
        missing.append("model/version_mismatch")
    required_runtime = RUNTIME_REQUIRED if model_version == "2.0" else (
        "python", "python/nltk_data", "vendor/index-tts/indextts/infer_v2_5.py",
    )
    missing.extend(_missing_under(rt, required_runtime, "runtime"))
    if not py.exists():
        missing.append("runtime/python")
    missing.extend(_missing_under(md, MODEL_25_REQUIRED if model_version == "2.5" else MODEL_REQUIRED, "model"))

    config_state = "runtime_package"
    config_source = "model_dir_cli_arg"
    if not cfg.exists():
        config_state = "missing"
    elif not os.access(cfg, os.R_OK):
        missing.append("model/config.yaml_unreadable")
        config_state = "error"

    runtime_ready = not any(item.startswith("runtime") for item in missing)
    model_ready = not any(item.startswith("model") for item in missing)
    if runtime_source == "managed" and model_source == "managed":
        # Files left by a partial or obsolete installation are not a ready model.
        if expected_version == "2.0":
            receipts = rp.mm.app_data_dir() / ".artifact-trust"
            # Use the same current/legacy locks as installation and the model page.
            # A hardcoded 20260629 lock rejected every newer valid 2.0 runtime.
            runtime_trust = rp._installed_runtime_manifest('local_indextts2_0')
            weight_trust = {"source": "hf", "repositories": [
                {"repo": repo, "revision": HF_MODEL_REVISIONS[repo]}
                for repo in ("IndexTeam/IndexTTS-2", "facebook/w2v-bert-2.0", "amphion/MaskGCT",
                             "funasr/campplus", "nvidia/bigvgan_v2_22khz_80band_256x")
            ]}
            runtime_ready = runtime_ready and runtime_trust is not None and install_receipt_valid("tts-runtime:local_indextts2", rt, runtime_trust, receipts)
            model_ready = model_ready and install_receipt_valid("tts-weights:local_indextts2", md, weight_trust, receipts)
        else:
            runtime_ready = runtime_ready and rp.is_runtime_installed(ENGINE_ID)
            model_ready = model_ready and rp.is_weights_installed(ENGINE_ID)
        if not runtime_ready and not any(item.startswith("runtime") for item in missing):
            missing.append("runtime/install_receipt_invalid")
        if not model_ready and not any(item.startswith("model") for item in missing):
            missing.append("model/install_receipt_invalid")
    config_ready = config_state == "runtime_package"
    runtime_size = _dir_size(rt) if include_size else 0
    model_size = _dir_size(md) if include_size else 0
    return IndexTTS2RuntimeStatus(
        runtime_dir=rt,
        model_dir=md,
        config_path=cfg,
        python_executable=py,
        cli_executable=cli,
        vendor_dir=vendor,
        nltk_data_dir=nltk,
        runtime_ready=runtime_ready,
        model_ready=model_ready,
        config_ready=config_ready,
        runtime_source=runtime_source,
        model_source=model_source,
        config_source=config_source,
        config_state=config_state,
        missing_items=tuple(missing),
        runtime_size_bytes=runtime_size,
        model_size_bytes=model_size,
        detail_redacted=scrub_detail("; ".join(missing)),
        model_version=model_version,
    )
