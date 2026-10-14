"""VoxCPM2 独立运行时包子进程 adapter(ADR-0015)。

主进程从不 import voxcpm:用运行时包里的 Python 跑 voxcpm2_worker.py,消费 JSONL。
取消异常复用 voxcpm2._Cancelled,路由透明(ui 层的 was_cancelled 仍识别)。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..engines.local_sensevoice import detect_device
from . import local_models
from . import runtime_packages as rp
from .base import SynthesisEngine, SynthesisError, SynthesisRequest, SynthesisResult, _noop
from .errors import scrub_detail
from .voxcpm2_options import validate_options, generation_inputs
from ..artifact_trust import HF_MODEL_REVISIONS
from ..audio.output_level import level_pcm16_wav
from .voxcpm2 import _Cancelled

ENGINE_ID = "local_voxcpm2"


def _worker_script() -> Path:
    return Path(__file__).with_name("voxcpm2_worker.py")


def _parse(line: str) -> dict[str, Any] | None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL) if os.name != "nt" else proc.kill()
        except Exception:  # noqa: BLE001
            proc.kill()
        proc.wait(timeout=5)


class VoxCPM2SubprocessEngine(SynthesisEngine):
    name = "local_voxcpm2"
    display_name = "VoxCPM2 本地克隆音(独立运行时包)"
    requires_network = False

    def synthesize(self, request: SynthesisRequest, progress=None, is_cancelled=None) -> SynthesisResult:
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        text = request.text or ""
        try:
            options = validate_options(request.engine_options)
            generate = generation_inputs(text, request.voice, options)
        except ValueError as exc:
            raise SynthesisError(str(exc)) from exc
        if is_cancelled():
            raise _Cancelled()
        if rp.status(ENGINE_ID) != "ready":
            raise SynthesisError("VoxCPM2 运行环境未就绪,请到 AI 大模型配置安装。")
        revision = HF_MODEL_REVISIONS[local_models.VOXCPM2.repo]
        model_path = local_models.hf_cache_dir(ENGINE_ID) / "models--openbmb--VoxCPM2" / "snapshots" / revision
        if not model_path.is_dir():
            raise SynthesisError("已校验的 VoxCPM2 模型快照不存在，请检查本地部署。")
        wav_path = request.output_path.with_suffix('.wav')
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="lulu_voxsub_"))
        payload = {
            "model_path": str(model_path),
            "cache_dir": str(local_models.hf_cache_dir(ENGINE_ID)),
            "device": detect_device(), "generate": generate,
            "seed": options['seed'],
            "output_path": str(tmpdir / 'result.wav'),
            "raw_path": str(tmpdir / 'raw.wav'),
        }
        reqfile = tmpdir / "request.json"
        reqfile.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HOME": payload["cache_dir"], "PYTHONNOUSERSITE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"}
        kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                                  "text": True, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                [str(rp.runtime_python(ENGINE_ID)), "-I", "-s", str(_worker_script()),
                 "--request-json", str(reqfile)], **kwargs,
            )
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

        q: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
        err_lines: list[str] = []

        def _read_out(stream):
            try:
                for line in iter(stream.readline, ""):
                    q.put(("out", line))
            finally:
                q.put(("out", None))

        def _read_err(stream):
            try:
                for line in iter(stream.readline, ""):
                    err_lines.append(line)
            finally:
                q.put(("err", None))

        threading.Thread(target=_read_out, args=(proc.stdout,), daemon=True).start()
        threading.Thread(target=_read_err, args=(proc.stderr,), daemon=True).start()

        result_ev: dict | None = None
        err_detail: str | None = None
        try:
            done = 0
            while done < 2:
                if is_cancelled():
                    _terminate(proc)
                    raise _Cancelled()
                try:
                    kind, line = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    done += 1
                    continue
                if kind != "out":
                    continue
                ev = _parse(line)
                if not ev:
                    continue
                if ev.get("type") == "progress":
                    progress(str(ev.get("message", "")), float(ev.get("frac", -1.0)))
                elif ev.get("type") == "result":
                    result_ev = ev
                elif ev.get("type") == "error":
                    err_detail = scrub_detail(str(ev.get("detail", "")))
            if proc.wait() != 0 or result_ev is None:
                raise SynthesisError(err_detail or scrub_detail("\n".join(err_lines[-20:])) or "VoxCPM2 生成失败。")
            def check_cancelled():
                if is_cancelled():
                    raise _Cancelled()
            check_cancelled()
            progress("整理成品音量…", .92)
            temporary = Path(payload['output_path'])
            output_level = level_pcm16_wav(temporary, check_cancelled=check_cancelled)
            check_cancelled()
            raw_path = wav_path.with_name(wav_path.stem + '.raw.wav')
            # Copy from the worker directory only after generation and leveling succeed.
            shutil.copy2(payload['raw_path'], raw_path)
            shutil.copy2(temporary, wav_path)
            output_path = wav_path
            if request.output_format == "mp3":
                from ..audio.converter import wav_to_mp3
                check_cancelled()
                progress("导出 MP3…", .96)
                output_path = request.output_path.with_suffix('.mp3')
                wav_to_mp3(wav_path, output_path)
            check_cancelled()
            progress("合成完成", 1.0)
            return SynthesisResult(
                output_path=output_path, wav_path=wav_path,
                duration_seconds=float(result_ev['duration']), engine=self.name,
                voice_id=request.voice.id if request.voice and options['mode'] != 'design' else '', text=text,
                meta={"segments": 1, "device": payload['device'], "runtime": "subprocess",
                      "mode": options['mode'], "options": options, "actual_input": generate['text'],
                      "reference_text": generate.get('prompt_text', ''),
                      "reference_audio_path": generate.get('reference_wav_path', ''),
                      "model_revision": revision, "raw_path": str(raw_path),
                      "sample_rate": result_ev['sample_rate'], "dtype": result_ev.get('dtype'),
                      "package_version": result_ev.get('package_version'),
                      "seconds": result_ev.get('seconds'), "output_level": output_level},
            )
        finally:
            if proc.poll() is None:
                _terminate(proc)
            shutil.rmtree(tmpdir, ignore_errors=True)
