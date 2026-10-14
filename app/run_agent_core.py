"""Frozen entry for the single current-user Lulu Agent Core."""

from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    if not bool(getattr(sys, "frozen", False)):
        raise RuntimeError("Agent Core只能由冻结构建启动")
    executable = Path(str(getattr(sys, "executable", "")))
    if not executable.is_absolute():
        raise RuntimeError("Agent Core冻结路径无效")
    # Local diagnostics: SIGUSR1 prints Python stacks only, never frame locals
    # or credential values. This lets a stalled native call be traced to its
    # Python caller without changing the running service or asking for secrets.
    from src.core.diagnostics import enable_core_stack_diagnostics
    enable_core_stack_diagnostics(Path.home() / "Library/Application Support/Lulu")
    resource_root = executable.parent.parent / "Resources"
    if sys.argv[1:2] in (['--migrate-legacy-tasks'], ['--inherit-legacy-tasks']):
        from src.core.agent_entry.local_task_storage import migration_entry
        return migration_entry(sys.argv[2:], automatic=sys.argv[1] == '--inherit-legacy-tasks')
    if sys.argv[1:2] == ['--prepare-update-checkpoint']:
        from src.core.updater.handover import checkpoint_entry
        return checkpoint_entry(sys.argv[2:])
    if sys.argv[1:2] == ["--prepare-install"]:
        from src.core.agent_entry.standalone_runtime import prepare_entry
        return prepare_entry(sys.argv[2:])
    if (resource_root/'standalone-install.v1.json').exists():
        import os
        from src.core.agent_entry.bundle_lifetime import watch_app_replacement
        # Old helpers know only the GUI PID. A rollback must not leave this
        # generation's Core alive against an App that has already been restored.
        watch_app_replacement(resource_root.parent.parent, lambda: os._exit(0))
    from src.core.agent_entry.entrypoints import run_core
    from src.core.agent_entry.requirements import project_component_identity

    identity = project_component_identity(
        resource_root=resource_root,
        adapter_id="agent_cli",
    )
    # Runtime construction remains inside Core. It may access the existing local
    # authorization/storage boundaries only after the measured handshake.
    from src.core.cli_runtime.factory import build_measured_core_runtime

    run_core(
        resource_root=resource_root,
        product_identity=identity,
        runtime_factory=lambda: build_measured_core_runtime(
            product_identity=identity,
            resource_root=resource_root,
        ),
    )
    return 1


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
