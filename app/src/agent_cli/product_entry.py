"""Qt-free product adapter for the packaged Agent CLI executable."""

from __future__ import annotations

from pathlib import Path
import json
import sys
from typing import TextIO

from ..core.product_identity import project_product_identity
from .contracts import write_cli_error
from .main import main as control_plane_main


def _resource_root(resource_root: Path | None) -> Path:
    if resource_root is not None:
        root = Path(resource_root)
    else:
        if not getattr(sys, "frozen", False):
            raise RuntimeError("Agent CLI产品适配器只能读取冻结资源")
        root = Path(str(getattr(sys, "_MEIPASS", "")))
    if not root.is_absolute():
        raise RuntimeError("Agent CLI冻结资源根必须是绝对路径")
    return root


def main(
    argv: list[str] | None = None,
    *,
    resource_root: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    local_transcription_runtime: object | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = _resource_root(resource_root)
    try:
        manifest_schema = json.loads(
            (root / "agent_product_manifest.json").read_text(encoding="utf-8")
        ).get("schema_id")
    except Exception:
        manifest_schema = None
    v2_declared = (root / "component_delivery.v2.json").exists() or (root / "agent_links.v2.json").exists()
    if v2_declared or manifest_schema == "lulu.agent-component-manifest/v1":
        from ..core.agent_entry.entrypoints import run_cli_via_core

        return run_cli_via_core(
            args,
            resource_root=root,
            stdout=stdout or sys.stdout,
            stderr=stderr or sys.stderr,
        )
    identity = project_product_identity(
        build_profile_path=root / "build_profile.json",
        manifest_path=root / "agent_product_manifest.json",
        adapter_id="agent_cli",
    )
    runtime = local_transcription_runtime
    runtime_error: tuple[str, str] | None = None
    if runtime is None and bool(getattr(sys, "frozen", False)):
        try:
            from ..core.cli_runtime.factory import build_production_runtime

            runtime = build_production_runtime(
                product_identity=identity,
                resource_root=root,
            )
        except ImportError:
            runtime_error = (
                "RUNTIME_NOT_READY",
                "PACKAGED_RUNTIME_IMPORT_FAILED",
            )
            runtime = None
        except RuntimeError as exc:
            if (
                type(getattr(exc, "code", None)) is str
                and type(getattr(exc, "reason_code", None)) is str
            ):
                runtime_error = (exc.code, exc.reason_code)
            else:
                runtime_error = (
                    "RUNTIME_NOT_READY",
                    "PACKAGED_RUNTIME_BOOT_FAILED",
                )
            runtime = None
    if (
        runtime is None
        and runtime_error is not None
        and args
        and args[0] in {"transcribe", "jobs"}
    ):
        command = ".".join(args[:2]) if len(args) >= 2 else args[0]
        return write_cli_error(
            code=runtime_error[0],
            command=command,
            details={"reason_code": runtime_error[1]},
            stdout=stdout or sys.stdout,
            stderr=stderr or sys.stderr,
        )
    return control_plane_main(
        args,
        stdout=stdout,
        stderr=stderr,
        product_identity=identity,
        resource_root=root,
        local_transcription_runtime=runtime,
    )
