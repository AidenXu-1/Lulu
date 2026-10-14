"""Internal IndexTTS2 experience command.

This is a low-exposure smoke/experience path for coordination use. It is not a
formal product UI and must keep IndexTTS2 isolated behind the subprocess adapter.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .core.synthesis.base import SynthesisRequest
from .core.synthesis.errors import scrub_detail, user_error_message
from .core.synthesis.indextts2_presets import EMOTION_PRESETS
from .core.synthesis.indextts2_subprocess import (
    IndexTTS2SubprocessEngine,
    check_runtime,
    was_cancelled,
)
from .core.synthesis.voices import get_voice, list_voices


DEFAULT_TEXT = "兆基你好，这是 Lulu 的 IndexTTS2 情感配音内部体验通路。今天我们先听一个开心一点的版本。"


def _progress(msg: str, frac: float) -> None:
    pct = f"{int(frac * 100):3d}%" if frac is not None and frac >= 0 else "  · "
    print(f"[{pct}] {msg}", file=sys.stderr, flush=True)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_runtime_dir() -> Path | None:
    candidate = _project_root() / "scratch" / "tts_indextts2" / "index-tts"
    return candidate if candidate.exists() else None


def _default_model_dir(runtime_dir: Path | None) -> Path | None:
    if runtime_dir is None:
        return None
    candidate = runtime_dir / "checkpoints"
    return candidate if candidate.exists() else None


def _pick_voice(voice_id: str | None):
    if voice_id:
        voice = get_voice(voice_id)
        if voice is None:
            raise RuntimeError(f"未找到音色:{voice_id}")
        return voice
    voices = list_voices()
    if not voices:
        raise RuntimeError("没有可用音色。请先在配音页创建一个音色。")
    return voices[0]


def _configure_runtime(args) -> tuple[Path | None, Path | None]:
    fallback_runtime = None if args.managed_only else _default_runtime_dir()
    runtime_dir = Path(args.runtime_dir).expanduser() if args.runtime_dir else fallback_runtime
    model_dir = Path(args.model_dir).expanduser() if args.model_dir else _default_model_dir(runtime_dir)
    if runtime_dir is not None:
        os.environ["LULU_INDEXTTS2_RUNTIME_DIR"] = str(runtime_dir)
    if model_dir is not None:
        os.environ["LULU_INDEXTTS2_MODEL_DIR"] = str(model_dir)
    return runtime_dir, model_dir


def _print_status(status) -> None:
    source = "开发/内部" if status.is_development else "受管"
    missing = ", ".join(status.missing_items) if status.missing_items else "无"
    print(f"状态:{'可用' if status.ready else '未准备'}", file=sys.stderr)
    print(f"来源:{source}", file=sys.stderr)
    print(f"runtime来源:{status.runtime_source}", file=sys.stderr)
    print(f"model来源:{status.model_source}", file=sys.stderr)
    print(f"config来源:{status.config_source}", file=sys.stderr)
    print(f"runtime可用:{status.runtime_ready}", file=sys.stderr)
    print(f"model可用:{status.model_ready}", file=sys.stderr)
    print(f"config可用:{status.config_ready}", file=sys.stderr)
    print(f"config状态:{status.config_state}", file=sys.stderr)
    print(f"runtime占用:{status.runtime_size_bytes} bytes", file=sys.stderr)
    print(f"model占用:{status.model_size_bytes} bytes", file=sys.stderr)
    print(f"缺失项:{missing}", file=sys.stderr)


def _print_scratch_adopt_hint() -> None:
    print("当前不自动复制 9GB+ runtime/model。若要把 scratch 复制到受管目录,请先 dry-run:", file=sys.stderr)
    print(".venv/bin/python -m src.internal_indextts2_experience --prepare-from-scratch", file=sys.stderr)
    print("确认空间和目标路径后再显式执行:", file=sys.stderr)
    print(".venv/bin/python -m src.internal_indextts2_experience --prepare-from-scratch --execute", file=sys.stderr)
    print("若目标已存在且确认要覆盖/同步,再额外加 --overwrite。", file=sys.stderr)


def _dir_size(path: Path, exclude_names: set[str] | None = None) -> int:
    exclude_names = exclude_names or set()
    if not path.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for item in path.rglob("*"):
        try:
            if any(part in exclude_names for part in item.relative_to(path).parts):
                continue
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


def _format_gb(size: int) -> str:
    return f"{size / (1024 ** 3):.2f}GB"


def _managed_runtime_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Lulu" / "runtimes" / "indextts2" / "index-tts"


def _managed_model_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Lulu" / "models" / "local_indextts2" / "checkpoints"


def _run_rsync(src: Path, dst: Path, *, exclude_checkpoints: bool, overwrite: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a"]
    if overwrite:
        cmd.append("--delete")
    if exclude_checkpoints:
        cmd += ["--exclude", "checkpoints/"]
    cmd += [f"{src}/", f"{dst}/"]
    subprocess.run(cmd, check=True)


def _prepare_from_scratch(*, execute: bool, overwrite: bool) -> int:
    scratch_runtime = _default_runtime_dir()
    if scratch_runtime is None:
        print("scratch runtime 不存在,无法本地复制。", file=sys.stderr)
        return 2
    scratch_model = scratch_runtime / "checkpoints"
    managed_runtime = _managed_runtime_dir()
    managed_model = _managed_model_dir()
    runtime_size = _dir_size(scratch_runtime, exclude_names={"checkpoints"})
    model_size = _dir_size(scratch_model)
    total_size = runtime_size + model_size
    free_bytes = shutil.disk_usage(managed_runtime.parent if managed_runtime.parent.exists() else Path.home()).free
    runtime_exists = managed_runtime.exists()
    model_exists = managed_model.exists()

    print("IndexTTS2 本地准备预检查", file=sys.stderr)
    print(f"源runtime:{scratch_runtime}", file=sys.stderr)
    print(f"源model:{scratch_model}", file=sys.stderr)
    print(f"目标runtime:{managed_runtime}", file=sys.stderr)
    print(f"目标model:{managed_model}", file=sys.stderr)
    print(f"预计runtime复制:{_format_gb(runtime_size)} (排除 checkpoints)", file=sys.stderr)
    print(f"预计model复制:{_format_gb(model_size)}", file=sys.stderr)
    print(f"预计总复制:{_format_gb(total_size)}", file=sys.stderr)
    print(f"目标磁盘可用:{_format_gb(free_bytes)}", file=sys.stderr)
    print(f"目标runtime已存在:{runtime_exists}", file=sys.stderr)
    print(f"目标model已存在:{model_exists}", file=sys.stderr)

    if not scratch_model.is_dir():
        print("源 checkpoints 不存在,请先准备 scratch 模型。", file=sys.stderr)
        return 2
    if free_bytes < int(total_size * 1.05):
        print("可用空间不足,未执行复制。", file=sys.stderr)
        return 3
    if not execute:
        if runtime_exists or model_exists:
            print("dry-run:目标已存在;真实执行会要求显式加 --overwrite。", file=sys.stderr)
        print("dry-run:未执行复制。确认后加 --execute 才会复制。", file=sys.stderr)
        return 0
    if (runtime_exists or model_exists) and not overwrite:
        print("目标已存在,未执行复制。需要覆盖时请显式加 --overwrite。", file=sys.stderr)
        return 4

    print("开始复制 runtime(排除 checkpoints)...", file=sys.stderr)
    _run_rsync(scratch_runtime, managed_runtime, exclude_checkpoints=True, overwrite=overwrite)
    print("开始复制 model checkpoints...", file=sys.stderr)
    _run_rsync(scratch_model, managed_model, exclude_checkpoints=False, overwrite=overwrite)
    status = check_runtime(managed_runtime, managed_model, runtime_source="managed", model_source="managed")
    _print_status(status)
    return 0 if status.ready else 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lulu 内部 IndexTTS2 体验入口(非正式 UI)")
    parser.add_argument("--voice-id", default=None, help="音色 ID;省略时使用音色库第一个")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="要生成的文本")
    parser.add_argument("--preset", default="happy", choices=sorted(EMOTION_PRESETS), help="情感预设")
    parser.add_argument("--output", default="../scratch/tts_indextts2/experience_hidden_entry.wav",
                        help="输出 wav 路径")
    parser.add_argument("--runtime-dir", default=None, help="IndexTTS2 runtime 目录;省略时尝试 scratch runtime")
    parser.add_argument("--model-dir", default=None, help="IndexTTS2 checkpoints 目录;省略时尝试 runtime/checkpoints")
    parser.add_argument("--device", default="mps", help="运行设备,如 mps/cpu")
    parser.add_argument("--timeout", type=float, default=600.0, help="生成超时时间(秒)")
    parser.add_argument("--managed-only", action="store_true",
                        help="不使用 scratch fallback,只检查默认受管目录或显式传入目录")
    parser.add_argument("--status", action="store_true", help="只检查 runtime/model/config 状态,不生成音频")
    parser.add_argument("--print-adopt-scratch-commands", action="store_true",
                        help="打印把当前 scratch runtime/model 复制到受管目录的命令,不执行")
    parser.add_argument("--prepare-from-scratch", action="store_true",
                        help="预检查/本地复制 scratch runtime/model 到受管目录;默认 dry-run")
    parser.add_argument("--execute", action="store_true", help="配合 --prepare-from-scratch 执行真实复制")
    parser.add_argument("--overwrite", action="store_true", help="目标已存在时允许覆盖/同步")
    args = parser.parse_args(argv)

    try:
        if args.prepare_from_scratch:
            return _prepare_from_scratch(execute=args.execute, overwrite=args.overwrite)
        runtime_dir, model_dir = _configure_runtime(args)
        status = check_runtime(include_size=args.status)
        if args.print_adopt_scratch_commands:
            _print_scratch_adopt_hint()
            return 0
        if args.status:
            _print_status(status)
            return 0 if status.ready else 2
        voice = _pick_voice(args.voice_id)
        print("Lulu IndexTTS2 内部体验入口(非正式 UI)", file=sys.stderr)
        print(f"音色:{voice.id} · {voice.name}", file=sys.stderr)
        print(f"情感预设:{args.preset}", file=sys.stderr)
        print(f"runtime:{'开发/内部' if runtime_dir else '默认受管目录'}", file=sys.stderr)
        print(f"model:{'开发/内部' if model_dir else '默认受管目录'}", file=sys.stderr)
        print(status.summary(), file=sys.stderr)
        if not status.ready:
            print(f"IndexTTS2 runtime/model 未准备:{status.detail_redacted or '请检查状态。'}", file=sys.stderr)
            return 2

        output_path = Path(args.output).expanduser()
        request = SynthesisRequest(
            text=args.text,
            voice=voice,
            output_path=output_path,
            output_format="wav",
            engine_options={
                "emotion_preset": args.preset,
                "emotion_weight": 0.8,
                "device": args.device,
                "timeout_seconds": args.timeout,
            },
        )
        result = IndexTTS2SubprocessEngine().synthesize(request, progress=_progress)
        print(f"已生成:{result.output_path}", file=sys.stderr)
        print(f"时长:{result.duration_seconds:.3f}s", file=sys.stderr)
        print(f"耗时:{result.meta.get('elapsed_seconds')}s", file=sys.stderr)
        print(f"情感:{result.meta.get('emotion_preset')}", file=sys.stderr)
        print(result.output_path)
        return 0
    except Exception as exc:  # noqa: BLE001
        if was_cancelled(exc):
            print("已取消。", file=sys.stderr)
            return 130
        print(user_error_message(exc, default="IndexTTS2 内部体验生成失败。"), file=sys.stderr)
        print(f"调试摘要:{scrub_detail(str(exc), limit=240)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
