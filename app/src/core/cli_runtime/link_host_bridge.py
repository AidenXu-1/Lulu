"""Bounded same-bundle IPC for one public-link human confirmation."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import selectors
import stat
import struct
import subprocess
import time
from typing import Mapping
from uuid import uuid4

from ..link_transcription_application import LinkTranscriptionError
from ..agent_trust.canonical import domain_separated_sha256


HOST_MODE_ARGUMENT = "--lulu-link-approval-host-v1"
MAX_HOST_MESSAGE_BYTES = 64 * 1024
HOST_TIMEOUT_SECONDS = 120.0
_REQUEST_FIELDS = frozenset(
    {"schema_id", "schema_revision", "request_id", "host_identity", "plan"}
)
_RESPONSE_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "request_id",
        "request_digest",
        "decision",
        "action_source",
        "action_nonce",
    }
)
_PLAN_FIELDS = frozenset(
    {
        "plan_version",
        "capability_ids",
        "installation_id",
        "typed_request",
        "effects",
        "targets",
        "selected_profiles",
        "idempotency",
        "missing_questions",
        "plan_digest",
    }
)
_TICKET_SEAL = object()
_BROKER_FACTORY_SEAL = object()


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_host_frame(payload: Mapping[str, object]) -> bytes:
    body = _canonical_json(payload)
    if not body or len(body) > MAX_HOST_MESSAGE_BYTES:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_OVERSIZE")
    return struct.pack(">I", len(body)) + body


def decode_host_frame(frame: bytes) -> dict[str, object]:
    if type(frame) is not bytes or len(frame) < 5:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_MALFORMED")
    length = struct.unpack(">I", frame[:4])[0]
    if length < 1 or length > MAX_HOST_MESSAGE_BYTES:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_OVERSIZE")
    if len(frame) != length + 4:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_TRAILING")
    body = frame[4:]
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - hostile child boundary
        raise LinkTranscriptionError(
            "HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_MALFORMED"
        ) from exc
    if type(payload) is not dict or _canonical_json(payload) != body:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_NONCANONICAL")
    return payload


def validate_host_request(payload: dict[str, object]) -> None:
    identity = payload.get("host_identity")
    plan = payload.get("plan")
    typed = plan.get("typed_request") if type(plan) is dict else None
    effects = plan.get("effects") if type(plan) is dict else None
    targets = plan.get("targets") if type(plan) is dict else None
    profiles = plan.get("selected_profiles") if type(plan) is dict else None
    idempotency = plan.get("idempotency") if type(plan) is dict else None
    if (
        frozenset(payload) != _REQUEST_FIELDS
        or payload.get("schema_id") != "lulu.link-approval-host-request/v1"
        or payload.get("schema_revision") != 1
        or type(payload.get("request_id")) is not str
        or type(identity) is not dict
        or frozenset(identity)
        != {
            "bundle_id",
            "product_version",
            "product_build",
            "profile",
            "source_commit",
            "source_tree",
            "identity_digest",
            "signature_digest",
        }
        or any(type(value) is not str or not value for value in identity.values())
        or type(plan) is not dict
        or frozenset(plan) != _PLAN_FIELDS
        or plan.get("plan_version") != "lulu.link-transcription-plan/v1"
        or type(plan.get("plan_digest")) is not str
        or type(typed) is not dict
        or frozenset(typed)
        != {
            "source",
            "engine",
            "format",
            "prefer_subtitles",
            "subtitle_fallback",
            "save_media",
            "media_mode",
            "media_directory",
            "save_cover",
            "output_destination",
        }
        or type(effects) is not dict
        or frozenset(effects)
        != {
            "profile",
            "network",
            "download_public_media",
            "download_subtitles",
            "run_local_model_on_fallback",
            "writes_shared_transcript",
            "writes_media",
            "writes_cover",
            "uploads_content",
            "may_charge",
        }
        or type(targets) is not dict
        or frozenset(targets)
        != {"transcript", "media_directory", "cover_destination"}
        or type(profiles) is not dict
        or frozenset(profiles) != {"effect", "job", "approval", "operation"}
        or type(idempotency) is not dict
        or frozenset(idempotency) != {"required_for_run", "binding"}
    ):
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_REQUEST_INVALID")
    expected_digest = domain_separated_sha256(
        "LULU_LINK_TRANSCRIPTION_PLAN_V1",
        {
            "plan_version": plan["plan_version"],
            "capability_ids": plan["capability_ids"],
            "installation_id": plan["installation_id"],
            "typed_request": typed,
            "effects": effects,
            "targets": targets,
            "selected_profiles": profiles,
            "idempotency": idempotency,
        },
    )
    if plan.get("plan_digest") != expected_digest:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_PLAN_DIGEST_INVALID")


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mode: int


def _owned_regular_identity(path: Path, reason: str) -> _FileIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", reason) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", reason)
    return _FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _resolve_gui_sibling(
    *,
    app_bundle: Path,
    cli_path: Path,
    expected_bundle_id: str,
    sealed_gui_executable: str,
    artifact_prefix: str,
) -> tuple[Path, _FileIdentity]:
    app = Path(app_bundle)
    cli = Path(cli_path)
    expected_cli = app / "Contents/MacOS/lulu-agent-cli"
    if cli != expected_cli or not app.is_absolute():
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_BUNDLE_IDENTITY_INVALID")
    _owned_regular_identity(cli, "HOST_CLI_IDENTITY_INVALID")
    info_path = app / "Contents/Info.plist"
    _owned_regular_identity(info_path, "HOST_INFO_PLIST_INVALID")
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001 - sealed bundle boundary
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_INFO_PLIST_INVALID") from exc
    executable_name = info.get("CFBundleExecutable")
    expected_relative = sealed_gui_executable.replace("{artifact_prefix}", artifact_prefix)
    if (
        info.get("CFBundleIdentifier") != expected_bundle_id
        or type(executable_name) is not str
        or not executable_name
        or Path(executable_name).name != executable_name
        or expected_relative != f"Contents/MacOS/{executable_name}"
    ):
        raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_BUNDLE_IDENTITY_INVALID")
    gui = app / expected_relative
    return gui, _owned_regular_identity(gui, "HOST_GUI_IDENTITY_INVALID")


class _PrivatePipeProcess:
    def __init__(self, process: subprocess.Popen[bytes], executable: Path) -> None:
        self._process = process
        self.executable = executable
        self.pid = process.pid
        self.birth_handle = object()

    def poll(self) -> int | None:
        return self._process.poll()

    def abort(self) -> None:
        self._terminate()

    def exchange(self, request: bytes, *, timeout: float, max_bytes: int) -> bytes:
        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_PIPE_UNAVAILABLE")
        deadline = time.monotonic() + timeout
        output = bytearray()
        request_offset = 0
        selector = selectors.DefaultSelector()
        os.set_blocking(stdin.fileno(), False)
        os.set_blocking(stdout.fileno(), False)
        selector.register(stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        try:
            eof = False
            write_closed = False
            while not (eof and write_closed):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate()
                    raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_TIMEOUT")
                events = selector.select(remaining)
                if not events:
                    self._terminate()
                    raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_TIMEOUT")
                for key, _events in events:
                    if key.data == "stdin":
                        try:
                            written = os.write(stdin.fileno(), request[request_offset:])
                        except (BrokenPipeError, OSError) as exc:
                            self._terminate()
                            raise LinkTranscriptionError(
                                "HUMAN_ACTION_REQUIRED", "HOST_PIPE_WRITE_FAILED"
                            ) from exc
                        request_offset += written
                        if request_offset == len(request):
                            selector.unregister(stdin)
                            stdin.close()
                            write_closed = True
                        continue
                    block = os.read(
                        stdout.fileno(),
                        min(4096, max_bytes + 1 - len(output)),
                    )
                    if not block:
                        selector.unregister(stdout)
                        eof = True
                        continue
                    output.extend(block)
                    if len(output) > max_bytes:
                        self._terminate()
                        raise LinkTranscriptionError(
                            "HUMAN_ACTION_REQUIRED", "HOST_MESSAGE_OVERSIZE"
                        )
        finally:
            selector.close()
            if not stdin.closed:
                stdin.close()
            if not stdout.closed:
                stdout.close()
        remaining = max(0.0, deadline - time.monotonic())
        try:
            exit_code = self._process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            self._terminate()
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_TIMEOUT") from exc
        if exit_code != 0:
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_CHILD_EXITED")
        return bytes(output)

    def _terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1.0)


def _start_private_pipe_process(
    *, executable: Path, argv: tuple[str, ...], environment: Mapping[str, str]
) -> _PrivatePipeProcess:
    process = subprocess.Popen(
        list(argv),
        executable=str(executable),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(environment),
        close_fds=True,
    )
    return _PrivatePipeProcess(process, executable)


class MacOSChildIdentityVerifier:
    """Bind the direct Popen birth handle to the expected on-disk executable."""

    def verify(
        self,
        *,
        process: object,
        expected_executable: Path,
        expected_identity: _FileIdentity,
    ) -> bool:
        if os.uname().sysname != "Darwin" or getattr(process, "poll", lambda: 1)() is not None:
            return False
        pid = getattr(process, "pid", None)
        if type(pid) is not int or pid <= 0 or getattr(process, "birth_handle", None) is None:
            return False
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            proc_pidpath = libproc.proc_pidpath
            proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            proc_pidpath.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            length = proc_pidpath(pid, buffer, len(buffer))
            if length <= 0:
                return False
            running = Path(os.fsdecode(buffer.value)).resolve(strict=True)
            expected = Path(expected_executable).resolve(strict=True)
            current = _owned_regular_identity(expected, "HOST_GUI_IDENTITY_INVALID")
        except Exception:  # noqa: BLE001 - fail closed at OS process boundary
            return False
        return running == expected and current == expected_identity


@dataclass(frozen=True, slots=True)
class _VerifiedHostActionClaims:
    issuer_id: str
    principal_id: str
    session_id: str
    origin_event_id: str
    nonce: str


class _VerifiedHostActionTicket:
    __slots__ = ("_claims", "_installation_id", "_plan_digest", "_used")

    def __init__(
        self,
        seal: object,
        *,
        installation_id: str,
        plan_digest: str,
        request_id: str,
        action_nonce: str,
    ) -> None:
        if seal is not _TICKET_SEAL:
            raise TypeError("verified host tickets are broker-issued")
        self._installation_id = installation_id
        self._plan_digest = plan_digest
        self._used = False
        self._claims = _VerifiedHostActionClaims(
            issuer_id="same_bundle_lulu_gui_broker",
            principal_id=f"local-user:{os.getuid()}",
            session_id=request_id,
            origin_event_id=f"pointer:{action_nonce}",
            nonce=action_nonce,
        )

    def consume(self, *, installation_id: str, plan_digest: str) -> _VerifiedHostActionClaims:
        if self._used:
            raise LinkTranscriptionError("APPROVAL_CONSUMED", "HOST_ACTION_TICKET_REPLAYED")
        if installation_id != self._installation_id or plan_digest != self._plan_digest:
            raise LinkTranscriptionError("APPROVAL_INVALID", "HOST_ACTION_TICKET_MISMATCH")
        self._used = True
        return self._claims


@dataclass(frozen=True, slots=True)
class LinkApprovalGrant:
    receipt: object
    approved_at: float
    idempotency_key: str


class LinkApprovalHostBroker:
    def __init__(
        self,
        factory_seal: object,
        *,
        app_bundle: Path,
        cli_path: Path,
        installation_id: str,
        expected_bundle_id: str,
        sealed_gui_executable: str,
        artifact_prefix: str,
        host_identity: Mapping[str, str],
    ) -> None:
        if factory_seal is not _BROKER_FACTORY_SEAL:
            raise LinkTranscriptionError(
                "HUMAN_ACTION_REQUIRED", "VERIFIED_RUNTIME_FACTORY_REQUIRED"
            )
        self._gui_path, self._gui_identity = _resolve_gui_sibling(
            app_bundle=app_bundle,
            cli_path=cli_path,
            expected_bundle_id=expected_bundle_id,
            sealed_gui_executable=sealed_gui_executable,
            artifact_prefix=artifact_prefix,
        )
        if any(type(value) is not str or not value for value in host_identity.values()):
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_BUILD_IDENTITY_INVALID")
        self._installation_id = installation_id
        self._host_identity = dict(host_identity)

    def request_approval(
        self,
        *,
        plan: dict[str, object],
        expected_idempotency_key: str | None = None,
    ) -> LinkApprovalGrant:
        request_id = _new_id("host-request")
        request_payload = {
            "schema_id": "lulu.link-approval-host-request/v1",
            "schema_revision": 1,
            "request_id": request_id,
            "host_identity": dict(self._host_identity),
            "plan": plan,
        }
        validate_host_request(request_payload)
        request_digest = hashlib.sha256(_canonical_json(request_payload)).hexdigest()
        request_frame = encode_host_frame(request_payload)
        if plan.get("installation_id") != self._installation_id:
            raise LinkTranscriptionError("APPROVAL_INVALID", "HOST_PLAN_INSTALLATION_MISMATCH")
        try:
            process = _start_private_pipe_process(
                executable=self._gui_path,
                argv=(str(self._gui_path), HOST_MODE_ARGUMENT),
                environment={},
            )
        except Exception as exc:  # noqa: BLE001 - process birth boundary
            raise LinkTranscriptionError(
                "HUMAN_ACTION_REQUIRED", "HOST_CHILD_START_FAILED"
            ) from exc
        verified = MacOSChildIdentityVerifier().verify(
            process=process,
            expected_executable=self._gui_path,
            expected_identity=self._gui_identity,
        )
        if verified is not True:
            abort = getattr(process, "abort", None)
            if callable(abort):
                abort()
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_CHILD_IDENTITY_INVALID")
        try:
            response_frame = process.exchange(
                request_frame,
                timeout=HOST_TIMEOUT_SECONDS,
                max_bytes=MAX_HOST_MESSAGE_BYTES + 4,
            )
        except LinkTranscriptionError:
            raise
        except Exception as exc:  # noqa: BLE001 - hostile/crashed child boundary
            raise LinkTranscriptionError(
                "HUMAN_ACTION_REQUIRED", "HOST_CHILD_CRASHED"
            ) from exc
        response = decode_host_frame(response_frame)
        self._validate_response(
            response,
            request_id=request_id,
            request_digest=request_digest,
        )
        decision = response["decision"]
        if decision == "cancelled":
            raise LinkTranscriptionError("CANCELLED", "HOST_USER_CANCELLED")
        if decision == "timed_out":
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_TIMEOUT")
        ticket = _VerifiedHostActionTicket(
            _TICKET_SEAL,
            installation_id=self._installation_id,
            plan_digest=str(plan["plan_digest"]),
            request_id=request_id,
            action_nonce=str(response["action_nonce"]),
        )
        approved_at = float(time.time())
        idempotency_key = expected_idempotency_key or _new_id("idempotency")
        from ..link_transcription_application import _issue_link_transcription_approval

        receipt = _issue_link_transcription_approval(
            plan=plan,
            verified_host_ticket=ticket,
            installation_id=self._installation_id,
            job_id=_new_id("job"),
            attempt_id=_new_id("attempt"),
            lineage_id=_new_id("lineage"),
            idempotency_key=idempotency_key,
            now=approved_at,
        )
        return LinkApprovalGrant(receipt, approved_at, idempotency_key)

    @staticmethod
    def _validate_response(
        response: dict[str, object], *, request_id: str, request_digest: str
    ) -> None:
        decision = response.get("decision")
        action_source = response.get("action_source")
        action_nonce = response.get("action_nonce")
        valid_decision = decision in {"confirmed", "cancelled", "timed_out"}
        confirmed = decision == "confirmed"
        if (
            frozenset(response) != _RESPONSE_FIELDS
            or response.get("schema_id") != "lulu.link-approval-host-response/v1"
            or response.get("schema_revision") != 1
            or response.get("request_id") != request_id
            or response.get("request_digest") != request_digest
            or not valid_decision
            or (confirmed and action_source != "spontaneous_pointer")
            or (not confirmed and action_source != "none")
            or type(action_nonce) is not str
            or (confirmed and not action_nonce)
            or (not confirmed and action_nonce != "")
        ):
            raise LinkTranscriptionError("HUMAN_ACTION_REQUIRED", "HOST_RESPONSE_INVALID")


def _new_id(kind: str) -> str:
    return f"{kind}-{uuid4().hex}"


def _build_production_link_host_broker(
    *,
    app_bundle: Path,
    cli_path: Path,
    installation_id: str,
    shared_identity: Mapping[str, object],
    identity_digest: str,
    signature_digest: str,
    artifact_prefix: str,
) -> LinkApprovalHostBroker:
    return LinkApprovalHostBroker(
        _BROKER_FACTORY_SEAL,
        app_bundle=app_bundle,
        cli_path=cli_path,
        installation_id=installation_id,
        expected_bundle_id=str(shared_identity.get("bundle_id", "")),
        sealed_gui_executable="Contents/MacOS/{artifact_prefix}",
        artifact_prefix=artifact_prefix,
        host_identity={
            "bundle_id": str(shared_identity.get("bundle_id", "")),
            "product_version": str(shared_identity.get("product_version", "")),
            "product_build": str(shared_identity.get("product_build", "")),
            "profile": str(shared_identity.get("profile", "")),
            "source_commit": str(shared_identity.get("source_commit", "")),
            "source_tree": str(shared_identity.get("source_tree", "")),
            "identity_digest": identity_digest,
            "signature_digest": signature_digest,
        },
    )
