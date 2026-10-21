"""Product-facing CLI/Core adapters for the measured generation-9 channel."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
import sys
from collections.abc import Callable
from typing import TextIO
from threading import RLock

from ...agent_cli.main import main as control_plane_main
from .native_transport import XpcBridge, XpcTransportError
from .protocol import AgentCoreProtocol, MeasuredCoreClient
from .requirements import load_gui_peer_requirements, load_peer_requirements
from .verification_cache import BundleVerificationCache
from .standalone_paths import installed_app, state_for_app, service_for_app


SERVICE_NAME = "com.lulu.transcribe.agent-core"
_default_gui_dispatcher = None
_default_gui_dispatcher_lock = RLock()


class GuiCoreDispatcher:
    """Open a measured GUI→Core connection for each bounded task request."""

    def __init__(self, *, app: Path, bridge: XpcBridge, authority_paths=()) -> None:
        self._app = app
        self._bridge = bridge
        self._verification_cache = BundleVerificationCache(app)
        self._authority_paths = tuple(authority_paths)
        self._gui_requirement, self._core_requirement, _verification = (
            self._verification_cache.verify(lambda: load_gui_peer_requirements(
                app,
                audit_session_id=bridge.audit_session_id,
            ), authority_paths=self._authority_paths)
        )

    def __call__(self, command: str, arguments: dict[str, object]) -> dict[str, object]:
        if (self._app / "Contents/Resources/component_delivery.v2.json").exists():
            self._gui_requirement, self._core_requirement, _ = self._verification_cache.verify(
                lambda: load_gui_peer_requirements(self._app, audit_session_id=self._bridge.audit_session_id),
                authority_paths=self._authority_paths,
            )
        with self._bridge.connect(service_for_app(self._app)) as connection:
            client = MeasuredCoreClient(
                exchange=connection,
                client_requirement=self._gui_requirement,
                core_requirement=self._core_requirement,
            )
            return client.dispatch(command, arguments)


def build_default_gui_core_dispatcher() -> GuiCoreDispatcher:
    """Construct only from the canonical frozen GUI and bundled transport."""
    if not bool(getattr(sys, "frozen", False)):
        raise RuntimeError("FROZEN_INSTALLATION_REQUIRED")
    executable = Path(str(sys.executable))
    app = executable.parent.parent.parent
    installed_app(app)
    if executable != app/"Contents/MacOS/Lulu":
        raise RuntimeError("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    global _default_gui_dispatcher
    with _default_gui_dispatcher_lock:
        if _default_gui_dispatcher is None:
            resource_root = app / "Contents/Resources"
            state = state_for_app(app)
            _default_gui_dispatcher = GuiCoreDispatcher(
                app=app,
                bridge=XpcBridge(resource_root / "liblulu_agent_xpc_bridge.dylib"),
                authority_paths=(state / "current.json", state / "ownership.json", state / "product-materials-v2"),
            )
        return _default_gui_dispatcher


def _app_bundle_from_resource_root(resource_root: Path) -> Path:
    root = Path(resource_root)
    if not root.is_absolute() or root.name != "Resources" or root.parent.name != "Contents":
        raise RuntimeError("AGENT_ENTRY_COMPONENT_MISMATCH")
    app = root.parent.parent
    installed_app(app)
    return app


def run_cli_via_core(
    argv: list[str],
    *,
    resource_root: Path,
    stdout: TextIO,
    stderr: TextIO,
    bridge: XpcBridge | None = None,
) -> int:
    app = _app_bundle_from_resource_root(resource_root)
    transport = bridge or XpcBridge(resource_root / "liblulu_agent_xpc_bridge.dylib")
    cli_requirement, core_requirement, _verification = load_peer_requirements(
        app,
        audit_session_id=transport.audit_session_id,
    )
    client = None
    try:
        with transport.connect(service_for_app(app)) as connection:
            client = MeasuredCoreClient(
                exchange=connection,
                client_requirement=cli_requirement,
                core_requirement=core_requirement,
            )
            result = client.dispatch("argv", {"argv": list(argv)})
    except XpcTransportError as exc:
        from ...agent_cli.contracts import write_cli_error

        dispatched = client is not None and client.business_dispatched is True
        if dispatched:
            code, reason = "CONFLICT", "CORE_REQUEST_OUTCOME_UNKNOWN"
        elif exc.native_code in {20, 21}:
            code = "RUNTIME_NOT_READY"
            reason = "CORE_TRANSPORT_TIMEOUT" if exc.native_code == 21 else "CORE_TRANSPORT_UNAVAILABLE"
        else:
            code, reason = "INTEGRITY_ERROR", "CORE_TRANSPORT_REJECTED"
        command = argv[0] if argv else "invalid"
        if command in {"tts", "jobs", "transcribe", "doctor", "skill"} and len(argv) > 1:
            command += "." + argv[1]
        return write_cli_error(
            code=code, command=command,
            details={"reason_code": reason, "native_code": exc.native_code,
                     "phase": "dispatched" if dispatched else "pre_dispatch",
                     "effect_count": "unknown" if dispatched else 0},
            stdout=stdout, stderr=stderr,
        )
    if frozenset(result) != {"exit_code", "stdout", "stderr"}:
        raise RuntimeError("CORE_COMPONENT_MISMATCH")
    if type(result["exit_code"]) is not int or type(result["stdout"]) is not str or type(result["stderr"]) is not str:
        raise RuntimeError("CORE_COMPONENT_MISMATCH")
    stdout.write(result["stdout"])
    stderr.write(result["stderr"])
    return result["exit_code"]


class CoreCommandDispatcher:
    def __init__(
        self,
        *,
        product_identity: dict[str, object],
        resource_root: Path,
        runtime_factory: Callable[[], object | None],
    ) -> None:
        self._identity = product_identity
        self._resource_root = Path(resource_root)
        self._external_delivery = (self._resource_root/"component_delivery.v2.json").exists() or (self._resource_root/"agent_links.v2.json").exists()
        self._runtime_factory = runtime_factory
        self._runtime: object | None = None
        self._runtime_loaded = False
        self._bootstrap_lock = RLock()
        self._verification_cache = BundleVerificationCache(self._resource_root.parent.parent)

    def _verify_installation(self, installation):
        paths = tuple(path for name, path in installation._all_targets().items() if name != 'app')
        return self._verification_cache.verify(installation.verify_installed, authority_paths=paths)

    def _initialize_runtime(self, installation) -> None:
        from .product_transaction import assert_business_available, product_state_lock
        with self._bootstrap_lock:
            if self._runtime_loaded:
                return
            with product_state_lock(installation.paths.state_root):
                assert_business_available(installation.paths.state_root)
                if self._external_delivery:
                    self._verify_installation(installation)
                self._runtime = self._runtime_factory()
                self._runtime_loaded = True

    def __call__(self, request: dict[str, object], *, _installation=None) -> dict[str, object]:
        from ..updater.handover import business_request_guard
        with business_request_guard(request):
            return self._dispatch(request, _installation=_installation)

    def _dispatch(self, request: dict[str, object], *, _installation=None) -> dict[str, object]:
        if frozenset(request) != {"command", "arguments"}:
            raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        arguments = request.get("arguments")
        command = request.get("command")
        if type(command) is not str or type(arguments) is not dict:
            raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")

        if command == 'gui.license':
            # Return a closed legacy reply so native transport does not turn a
            # deliberate retirement into an ambiguous retryable server error.
            # No license module, installation or credential is initialized.
            return {
                "allowed": False,
                "code": "feature_retired",
                "message": "授权码功能已退役；新版 Lulu 无需激活。",
                "active_device_count": None,
                "max_devices": None,
                "request_id": None,
                "deactivation_status": None,
                "replayed": False,
                "low_privilege_actions": [],
            }
        elif command == "gui.library":
            from ..library_gateway import validate_request
            validate_request(arguments)
        elif command == "gui.live":
            from ..live_recording.contracts import validate_request
            if set(arguments) != {"operation", "arguments"}:
                raise ValueError("直播请求字段无效")
            validate_request(**arguments)
        elif command == "gui.workspace":
            from ..agent_workspace.service import validate_request
            if set(arguments) != {"operation", "arguments"}:
                raise ValueError("工作区请求字段无效")
            validate_request(**arguments)
        elif command == "gui.jobs.batch":
            import json
            operations = arguments.get("operations")
            if (set(arguments) != {"operations"} or type(operations) is not list
                    or not 1 <= len(operations) <= 512
                    or len(json.dumps(arguments).encode("utf-8")) > 1024 * 1024
                    or any(type(op) is not dict or set(op) != {"command", "arguments"}
                           or op["command"] not in {"gui.jobs.create", "gui.jobs.transition"}
                           for op in operations)):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "gui.jobs.view":
            if set(arguments) != {"groups", "hidden_ids", "expected_revision"}:
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "gui.jobs.list":
            if arguments and (set(arguments) != {"include_view"} or type(arguments["include_view"]) is not bool):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "gui.jobs.result":
            if (set(arguments) != {"job_id", "offset"}
                    or type(arguments["job_id"]) is not str or not arguments["job_id"]
                    or type(arguments["offset"]) is not int
                    or not 0 <= arguments["offset"] < 32 * 1024 * 1024):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "gui.jobs.create":
            if set(arguments) not in ({"task"}, {"task", "request_id"}) or not isinstance(arguments["task"], dict):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "gui.jobs.transition":
            if set(arguments) - {"owner_pid", "error_message", "execution_policy"} != {
                "job_id",
                "expected_revision",
                "state",
                "result_ref",
            }:
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
            if "owner_pid" in arguments and (type(arguments["owner_pid"]) is not int or arguments["owner_pid"] <= 0):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
            if arguments.get("error_message") is not None and (
                    type(arguments["error_message"]) is not str or len(arguments["error_message"]) > 8000):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
            if (
                type(arguments["job_id"]) is not str
                or not arguments["job_id"]
                or type(arguments["expected_revision"]) is not int
                or arguments["expected_revision"] < 1
                or type(arguments["state"]) is not str
                or (
                    arguments["result_ref"] is not None
                    and type(arguments["result_ref"]) is not str
                )
            ):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        elif command == "argv":
            argv = arguments.get("argv")
            if set(arguments) != {"argv"} or type(argv) is not list or any(
                type(value) is not str for value in argv
            ):
                raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")
        else:
            raise RuntimeError("COMPONENT_EVIDENCE_REQUIRED")

        if command == "argv" and argv[:1] == ["auth"]:
            stdout, stderr = StringIO(), StringIO()
            exit_code = control_plane_main(argv, stdout=stdout, stderr=stderr,
                product_identity=self._identity, resource_root=self._resource_root)
            return {"exit_code": exit_code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}

        from .product_install import build_default_product_installer
        from .product_transaction import assert_business_available
        installation = _installation or build_default_product_installer()
        assert_business_available(installation.paths.state_root)
        if self._external_delivery and _installation is None:
            self._verify_installation(installation)
        if command == "argv" and argv == ["version", "--output", "json"]:
            stdout, stderr = StringIO(), StringIO()
            # Installation and measured transport already verified this entry.
            # Reading version metadata must not bootstrap jobs or model services.
            exit_code = control_plane_main(argv, stdout=stdout, stderr=stderr,
                product_identity=self._identity, resource_root=self._resource_root,
                verified_control_entry=True)
            return {"exit_code": exit_code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}

        if not self._runtime_loaded:
            from ..cli_runtime.contracts import CliRuntimeTrustError
            try:
                self._initialize_runtime(installation)
            except CliRuntimeTrustError as exc:
                if command != "argv":
                    raise
                from ...agent_cli.contracts import write_cli_error
                stdout, stderr = StringIO(), StringIO()
                exit_code = write_cli_error(
                    code="INTEGRITY_ERROR" if exc.code == "TRUST_ANCHOR_INVALID" else exc.code,
                    command=".".join(argv[:2]) if argv else "invalid",
                    details={"reason_code": exc.reason_code},
                    stdout=stdout,
                    stderr=stderr,
                )
                return {"exit_code": exit_code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
        if command == "gui.library":
            from ..library_gateway import handle
            return handle(arguments)
        if command == "gui.live":
            from ..live_recording.contracts import LiveError
            try:
                return {"result": self._runtime.live_recording().handle(**arguments)}
            except LiveError as exc:
                return {"error": str(exc)}
            except Exception:
                return {"error": "直播操作暂时无法完成，原资料保留，请重试"}
        if command == "gui.workspace":
            from ..agent_workspace.service import handle
            from ..library import RevisionConflict
            # The measured transport requires an object at its outer boundary;
            # workspace operations may return lists, scalars or null inside it.
            try:
                return {"result": handle(**arguments, runtime=self._runtime)}
            except (ValueError, FileNotFoundError, RevisionConflict) as exc:
                # Only business errors after the verified boundary travel as
                # replies. Installation/peer checks above still fail closed.
                return {"error": {"code": type(exc).__name__, "message": str(exc)[:2000]}}
        if command == "gui.jobs.batch":
            results = []
            for operation in operations:
                try:
                    results.append(self(operation, _installation=installation))
                except Exception as exc:
                    from ..diagnostics import record_failure, failure_code
                    record_failure("core_task_batch", exc, root=installation.paths.state_root)
                    return {"results": results, "error": failure_code(exc)}
            return {"results": results, "error": None}
        if command == "gui.jobs.list":
            return self._runtime.gui_jobs_list(**arguments)
        if command == "gui.jobs.view":
            return self._runtime.gui_jobs_view(**arguments)
        if command == "gui.jobs.result":
            return self._runtime.gui_job_result(**arguments)
        if command == "gui.jobs.create":
            return self._runtime.gui_jobs_create(**arguments)
        if command == "gui.jobs.transition":
            return self._runtime.gui_jobs_transition(**arguments)
        stdout = StringIO()
        stderr = StringIO()
        exit_code = control_plane_main(
            list(argv),
            stdout=stdout,
            stderr=stderr,
            product_identity=self._identity,
            resource_root=self._resource_root,
            local_transcription_runtime=self._runtime,
        )
        return {"exit_code": exit_code, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}


def run_core(
    *,
    resource_root: Path,
    product_identity: dict[str, object],
    runtime_factory: Callable[[], object | None],
    bridge: XpcBridge | None = None,
) -> None:
    app = _app_bundle_from_resource_root(resource_root)
    transport = bridge or XpcBridge(resource_root / "liblulu_agent_xpc_bridge.dylib")
    cli_requirement, _core_requirement, _verification = load_peer_requirements(
        app,
        audit_session_id=transport.audit_session_id,
    )
    gui_requirement, _gui_core_requirement, _gui_verification = load_gui_peer_requirements(
        app,
        audit_session_id=transport.audit_session_id,
    )
    protocol = AgentCoreProtocol(
        client_requirement=(cli_requirement, gui_requirement),
        dispatcher=CoreCommandDispatcher(
            product_identity=product_identity,
            resource_root=resource_root,
            runtime_factory=runtime_factory,
        ),
    )
    transport.run_server(service_for_app(app), protocol.handle)
