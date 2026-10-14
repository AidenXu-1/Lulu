from __future__ import annotations

from io import StringIO
from contextlib import contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


@contextmanager
def approved_host_broker(
    *,
    installation_id: str,
    now: float,
    ids: dict[str, str] | None = None,
):
    import src.core.cli_runtime.link_host_bridge as bridge
    from src.core.cli_runtime.link_host_bridge import decode_host_frame, encode_host_frame

    with TemporaryDirectory(prefix="lulu-r2-approved-host-") as directory:
        app = Path(directory) / "Lulu Trusted Test.app"
        macos = app / "Contents/MacOS"
        macos.mkdir(parents=True)
        cli = macos / "lulu-agent-cli"
        gui = macos / "Lulu-Trusted-Test"
        cli.write_bytes(b"cli")
        gui.write_bytes(b"gui")
        cli.chmod(0o755)
        gui.chmod(0o755)
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Lulu-Trusted-Test",
                    "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                },
                handle,
            )
        sequence = iter(range(1, 100))

        class FakeProcess:
            pid = 8113
            birth_handle = object()

            def poll(self):
                return None

            def exchange(self, request: bytes, *, timeout: float, max_bytes: int):
                del timeout, max_bytes
                payload = decode_host_frame(request)
                digest = hashlib.sha256(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                return encode_host_frame(
                    {
                        "schema_id": "lulu.link-approval-host-response/v1",
                        "schema_revision": 1,
                        "request_id": payload["request_id"],
                        "request_digest": digest,
                        "decision": "confirmed",
                        "action_source": "spontaneous_pointer",
                        "action_nonce": f"nonce-{installation_id}-{payload['request_id']}",
                    }
                )

        def id_factory(kind: str) -> str:
            if ids and kind in ids:
                return ids[kind]
            return f"{kind}-test-{next(sequence)}"

        with (
            patch.object(
                bridge,
                "_start_private_pipe_process",
                side_effect=lambda **_kwargs: FakeProcess(),
            ),
            patch.object(
                bridge.MacOSChildIdentityVerifier,
                "verify",
                return_value=True,
            ),
            patch.object(bridge.time, "time", return_value=now),
            patch.object(bridge, "_new_id", side_effect=id_factory),
        ):
            yield bridge._build_production_link_host_broker(
                app_bundle=app,
                cli_path=cli,
                installation_id=installation_id,
                shared_identity={
                    "bundle_id": "com.lulu.transcribe.trusted-test",
                    "product_version": "test",
                    "product_build": "test",
                    "profile": "trusted_test",
                    "source_commit": "test",
                    "source_tree": "test",
                },
                identity_digest="test",
                signature_digest="test",
                artifact_prefix="Lulu-Trusted-Test",
            )


@contextmanager
def host_broker_fixture(*, process_starter, verifier=lambda **_kwargs: True):
    import src.core.cli_runtime.link_host_bridge as bridge

    with TemporaryDirectory(prefix="lulu-r2-hostile-host-") as directory:
        app = Path(directory) / "Lulu Trusted Test.app"
        macos = app / "Contents/MacOS"
        macos.mkdir(parents=True)
        cli = macos / "lulu-agent-cli"
        gui = macos / "Lulu-Trusted-Test"
        cli.write_bytes(b"cli")
        gui.write_bytes(b"gui")
        cli.chmod(0o755)
        gui.chmod(0o755)
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Lulu-Trusted-Test",
                    "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                },
                handle,
            )
        sequence = iter(range(1, 100))
        with (
            patch.object(
                bridge,
                "_start_private_pipe_process",
                side_effect=process_starter,
            ),
            patch.object(
                bridge.MacOSChildIdentityVerifier,
                "verify",
                side_effect=verifier,
            ),
            patch.object(bridge.time, "time", return_value=1_777_500_000.0),
            patch.object(
                bridge,
                "_new_id",
                side_effect=lambda kind: f"{kind}-hostile-{next(sequence)}",
            ),
        ):
            yield bridge._build_production_link_host_broker(
                app_bundle=app,
                cli_path=cli,
                installation_id="installation-r2-hostile",
                shared_identity={
                    "bundle_id": "com.lulu.transcribe.trusted-test",
                    "product_version": "test",
                    "product_build": "test",
                    "profile": "trusted_test",
                    "source_commit": "test",
                    "source_tree": "test",
                },
                identity_digest="test",
                signature_digest="test",
                artifact_prefix="Lulu-Trusted-Test",
            )


class CliUrlHostBridgeRedTests(unittest.TestCase):
    def test_production_module_exposes_no_caller_injectable_authority_surface(self) -> None:
        import src.core.cli_runtime.link_host_bridge as bridge

        forbidden = {
            "process_starter",
            "child_identity_verifier",
            "verified_response",
            "confirmed_response",
            "now",
            "id_factory",
        }
        exposed: list[str] = []
        for name, value in vars(bridge).items():
            if name.startswith("_") or not (inspect.isclass(value) or inspect.isfunction(value)):
                continue
            try:
                parameters = set(inspect.signature(value).parameters)
            except (TypeError, ValueError):
                parameters = set()
            if parameters & forbidden:
                exposed.append(f"{name}({','.join(sorted(parameters & forbidden))})")
            if inspect.isclass(value):
                for method_name, method in vars(value).items():
                    if method_name.startswith("_") or not callable(method):
                        continue
                    try:
                        method_parameters = set(inspect.signature(method).parameters)
                    except (TypeError, ValueError):
                        continue
                    if method_parameters & forbidden:
                        exposed.append(
                            f"{name}.{method_name}"
                            f"({','.join(sorted(method_parameters & forbidden))})"
                        )

        self.assertEqual(exposed, [])

    def test_old_fake_broker_cannot_mint_receipt_or_execute_without_gui(self) -> None:
        import src.core.cli_runtime.link_host_bridge as bridge
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )
        from src.core.models import Segment, Transcript

        mutations: list[str] = []

        class Backend:
            def start(self, **_kwargs):
                mutations.append("job:start")
                return {"job_revision": 3, "lease_epoch": 1}

            def complete_success(self, *, claims, result_ref):
                mutations.append("job:success")
                return {
                    "job_id": claims.job_id,
                    "attempt_id": claims.attempt_id,
                    "lineage_id": claims.lineage_id,
                    "job_revision": 4,
                    "lease_epoch": 1,
                    "state": "succeeded",
                    "result_ref": result_ref,
                }

            def complete_failure(self, **_kwargs):
                mutations.append("job:failure")

            def mark_recovery_required(self, **_kwargs):
                mutations.append("job:recovery")

        with TemporaryDirectory(prefix="lulu-r3-fake-authority-") as directory:
            app = Path(directory) / "Lulu Trusted Test.app"
            macos = app / "Contents/MacOS"
            macos.mkdir(parents=True)
            cli = macos / "lulu-agent-cli"
            gui = macos / "Lulu-Trusted-Test"
            cli.write_bytes(b"cli")
            gui.write_bytes(b"gui")
            cli.chmod(0o755)
            gui.chmod(0o755)
            with (app / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleExecutable": "Lulu-Trusted-Test",
                        "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                    },
                    handle,
                )
            service = LinkTranscriptionApplicationService(
                installation_id="installation-r3-fake",
                pipeline_runner=lambda *, request, **_kwargs: (
                    mutations.append("network:pipeline")
                    or Transcript(
                        text="不应执行",
                        segments=[Segment("不应执行", 0.0, 1.0)],
                        language="zh",
                        engine="local_sensevoice",
                        source=request.source,
                    )
                ),
                job_backend=Backend(),
                result_writer=lambda *_args: (
                    mutations.append("disk:result")
                    or {
                        "result_ref": "lulu-result:r3:sha256:" + "a" * 64,
                        "content_hash": "a" * 64,
                    }
                ),
            )
            plan = service.prepare("https://example.com/r3-fake")

            def attempt_old_exploit() -> None:
                broker_type = getattr(bridge, "LinkApprovalHostBroker", None)
                old_factory = getattr(broker_type, "for_test", None)
                if not callable(old_factory):
                    raise LinkTranscriptionError(
                        "HUMAN_ACTION_REQUIRED", "CALLER_BROKER_FACTORY_UNAVAILABLE"
                    )

                class FakeProcess:
                    pid = 9911
                    birth_handle = object()

                    def poll(self):
                        return None

                    def exchange(self, request, **_kwargs):
                        payload = bridge.decode_host_frame(request)
                        request_digest = hashlib.sha256(
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        return bridge.encode_host_frame(
                            {
                                "schema_id": "lulu.link-approval-host-response/v1",
                                "schema_revision": 1,
                                "request_id": payload["request_id"],
                                "request_digest": request_digest,
                                "decision": "confirmed",
                                "action_source": "spontaneous_pointer",
                                "action_nonce": "nonce-r3-fake-no-gui",
                            }
                        )

                broker = old_factory(
                    app_bundle=app,
                    cli_path=cli,
                    installation_id="installation-r3-fake",
                    expected_bundle_id="com.lulu.transcribe.trusted-test",
                    sealed_gui_executable="Contents/MacOS/{artifact_prefix}",
                    artifact_prefix="Lulu-Trusted-Test",
                    process_starter=lambda **_kwargs: FakeProcess(),
                    child_identity_verifier=lambda **_kwargs: True,
                    now=lambda: 1_777_600_000.0,
                    id_factory=lambda kind: f"{kind}-r3-fake",
                )
                grant = broker.request_approval(plan=plan)
                service.run(
                    "https://example.com/r3-fake",
                    approval_receipt=grant.receipt,
                    now=grant.approved_at,
                )

            with self.assertRaises(LinkTranscriptionError) as blocked:
                attempt_old_exploit()
            self.assertEqual(blocked.exception.code, "HUMAN_ACTION_REQUIRED")
            with self.assertRaises(LinkTranscriptionError) as direct:
                bridge.LinkApprovalHostBroker(
                    object(),
                    app_bundle=app,
                    cli_path=cli,
                    installation_id="installation-r3-fake",
                    expected_bundle_id="com.lulu.transcribe.trusted-test",
                    sealed_gui_executable="Contents/MacOS/{artifact_prefix}",
                    artifact_prefix="Lulu-Trusted-Test",
                    host_identity={
                        "bundle_id": "com.lulu.transcribe.trusted-test",
                        "product_version": "test",
                        "product_build": "test",
                        "profile": "trusted_test",
                        "source_commit": "test",
                        "source_tree": "test",
                        "identity_digest": "test",
                        "signature_digest": "test",
                    },
                )
            self.assertEqual(
                direct.exception.reason_code,
                "VERIFIED_RUNTIME_FACTORY_REQUIRED",
            )
            self.assertEqual(mutations, [])

    def test_run_without_production_host_broker_is_human_action_required(self) -> None:
        from src.agent_cli.main import main
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
        )

        mutations: list[str] = []
        service = LinkTranscriptionApplicationService(
            installation_id="installation-r2-red",
            pipeline_runner=lambda **_kwargs: mutations.append("pipeline"),
            job_backend=lambda **_kwargs: mutations.append("job"),
            result_writer=lambda *_args: mutations.append("result"),
        )
        stdout = StringIO()

        exit_code = main(
            [
                "transcribe",
                "run",
                "https://example.com/watch?private=red",
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=StringIO(),
            link_transcription_service=service,
        )

        self.assertEqual(exit_code, 11)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "HUMAN_ACTION_REQUIRED",
        )
        self.assertEqual(mutations, [])

    def test_core_plain_dictionary_cannot_issue_approval(self) -> None:
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
            _issue_link_transcription_approval,
        )

        service = LinkTranscriptionApplicationService(
            installation_id="installation-r2-plain-dict",
        )
        plan = service.prepare("https://example.com/watch?v=plain")
        with self.assertRaises((TypeError, LinkTranscriptionError)):
            _issue_link_transcription_approval(
                plan=plan,
                verified_host_action={
                    "verified": True,
                    "issuer_id": "caller-dict",
                    "audience": "com.lulu.transcribe.core/link-approval/v1",
                    "actor_class": "authenticated_human_user",
                    "input_source": "trusted_gui_pointer_action",
                    "principal_id": "caller",
                    "session_id": "caller",
                    "origin_event_id": "caller",
                    "nonce": "r2-plain-dict-nonce",
                },
                installation_id="installation-r2-plain-dict",
                job_id="job-r2-plain",
                attempt_id="attempt-r2-plain",
                lineage_id="lineage-r2-plain",
                idempotency_key="idem-r2-plain",
                now=1_777_400_000.0,
            )

    def test_bridge_keeps_url_out_of_child_argv_and_environment(self) -> None:
        import src.core.cli_runtime.link_host_bridge as bridge

        with TemporaryDirectory(prefix="lulu-r2-host-red-") as directory:
            app = Path(directory) / "Lulu Trusted Test.app"
            macos = app / "Contents/MacOS"
            macos.mkdir(parents=True)
            cli = macos / "lulu-agent-cli"
            gui = macos / "Lulu-Trusted-Test"
            cli.write_bytes(b"cli")
            gui.write_bytes(b"gui")
            cli.chmod(0o755)
            gui.chmod(0o755)
            with (app / "Contents/Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleExecutable": "Lulu-Trusted-Test",
                        "CFBundleIdentifier": "com.lulu.transcribe.trusted-test",
                    },
                    handle,
                )
            launched: list[dict[str, object]] = []

            class FakeProcess:
                pid = 8112

                def exchange(self, request: bytes, *, timeout: float, max_bytes: int):
                    launched[-1]["request"] = request
                    raise RuntimeError("stop after capture")

            def start(*, executable, argv, environment):
                launched.append(
                    {
                        "executable": executable,
                        "argv": tuple(argv),
                        "environment": dict(environment),
                    }
                )
                return FakeProcess()

            from src.core.link_transcription_application import (
                LinkTranscriptionApplicationService,
            )

            plan = LinkTranscriptionApplicationService(
                installation_id="installation-r2-private-pipe"
            ).prepare("https://example.com/watch?secret=query")

            with (
                patch.object(
                    bridge,
                    "_start_private_pipe_process",
                    side_effect=start,
                ),
                patch.object(
                    bridge.MacOSChildIdentityVerifier,
                    "verify",
                    return_value=True,
                ),
                patch.object(bridge.time, "time", return_value=1_777_400_000.0),
                patch.object(
                    bridge,
                    "_new_id",
                    side_effect=lambda kind: f"{kind}-r2-private-pipe",
                ),
            ):
                broker = bridge._build_production_link_host_broker(
                    app_bundle=app,
                    cli_path=cli,
                    installation_id="installation-r2-private-pipe",
                    shared_identity={
                        "bundle_id": "com.lulu.transcribe.trusted-test",
                        "product_version": "test",
                        "product_build": "test",
                        "profile": "trusted_test",
                        "source_commit": "test",
                        "source_tree": "test",
                    },
                    identity_digest="test",
                    signature_digest="test",
                    artifact_prefix="Lulu-Trusted-Test",
                )
                with self.assertRaises(RuntimeError):
                    broker.request_approval(plan=plan)

            serialized_argv = json.dumps(launched[0]["argv"])
            serialized_environment = json.dumps(launched[0]["environment"])
            self.assertNotIn("example.com", serialized_argv)
            self.assertNotIn("example.com", serialized_environment)
            self.assertIn(b"example.com", launched[0]["request"])
            self.assertEqual(launched[0]["executable"], gui)

    def test_hostile_child_results_fail_closed_before_core_receipt(self) -> None:
        import struct

        from src.core.cli_runtime.link_host_bridge import (
            MAX_HOST_MESSAGE_BYTES,
            decode_host_frame,
            encode_host_frame,
        )
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )

        plan = LinkTranscriptionApplicationService(
            installation_id="installation-r2-hostile"
        ).prepare("https://example.com/watch?private=hostile")

        def valid_response(request: bytes, decision: str = "confirmed") -> dict[str, object]:
            payload = decode_host_frame(request)
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return {
                "schema_id": "lulu.link-approval-host-response/v1",
                "schema_revision": 1,
                "request_id": payload["request_id"],
                "request_digest": digest,
                "decision": decision,
                "action_source": "spontaneous_pointer" if decision == "confirmed" else "none",
                "action_nonce": "nonce-hostile-valid" if decision == "confirmed" else "",
            }

        class FakeProcess:
            pid = 9001
            birth_handle = object()

            def __init__(self, responder):
                self.responder = responder

            def poll(self):
                return None

            def exchange(self, request, **_kwargs):
                return self.responder(request)

        responders = {
            "crash": lambda _request: (_ for _ in ()).throw(RuntimeError("crash")),
            "malformed": lambda _request: b"bad",
            "trailing": lambda request: encode_host_frame(valid_response(request)) + b"x",
            "multi_message": lambda request: encode_host_frame(valid_response(request)) * 2,
            "oversize": lambda _request: struct.pack(">I", MAX_HOST_MESSAGE_BYTES + 1),
            "unknown_field": lambda request: encode_host_frame(
                {**valid_response(request), "unknown": True}
            ),
            "programmatic": lambda request: encode_host_frame(
                {**valid_response(request), "action_source": "programmatic"}
            ),
        }
        for case, responder in responders.items():
            with self.subTest(case=case), host_broker_fixture(
                process_starter=lambda responder=responder, **_kwargs: FakeProcess(
                    responder
                )
            ) as broker:
                with self.assertRaises(LinkTranscriptionError) as blocked:
                    broker.request_approval(plan=plan)
                self.assertEqual(blocked.exception.code, "HUMAN_ACTION_REQUIRED")

        for decision, expected_code in (
            ("cancelled", "CANCELLED"),
            ("timed_out", "HUMAN_ACTION_REQUIRED"),
        ):
            with self.subTest(decision=decision), host_broker_fixture(
                process_starter=lambda decision=decision, **_kwargs: FakeProcess(
                    lambda request: encode_host_frame(valid_response(request, decision))
                )
            ) as broker:
                with self.assertRaises(LinkTranscriptionError) as blocked:
                    broker.request_approval(plan=plan)
                self.assertEqual(blocked.exception.code, expected_code)

    def test_pid_executable_drift_and_action_nonce_replay_are_rejected(self) -> None:
        from src.core.cli_runtime.link_host_bridge import (
            decode_host_frame,
            encode_host_frame,
        )
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )

        plan = LinkTranscriptionApplicationService(
            installation_id="installation-r2-hostile"
        ).prepare("https://example.com/replay")

        class FakeProcess:
            pid = 9002
            birth_handle = object()

            def __init__(self) -> None:
                self.aborted = False
                self.exchange_calls = 0

            def poll(self):
                return None

            def abort(self):
                self.aborted = True

            def exchange(self, request, **_kwargs):
                self.exchange_calls += 1
                payload = decode_host_frame(request)
                digest = hashlib.sha256(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                return encode_host_frame(
                    {
                        "schema_id": "lulu.link-approval-host-response/v1",
                        "schema_revision": 1,
                        "request_id": payload["request_id"],
                        "request_digest": digest,
                        "decision": "confirmed",
                        "action_source": "spontaneous_pointer",
                        "action_nonce": "nonce-r2-fixed-replay",
                    }
                )

        drifted = FakeProcess()
        with host_broker_fixture(
            process_starter=lambda **_kwargs: drifted,
            verifier=lambda **_kwargs: False,
        ) as broker:
            with self.assertRaises(LinkTranscriptionError) as blocked:
                broker.request_approval(plan=plan)
        self.assertEqual(blocked.exception.reason_code, "HOST_CHILD_IDENTITY_INVALID")
        self.assertTrue(drifted.aborted)
        self.assertEqual(drifted.exchange_calls, 0)

        with host_broker_fixture(
            process_starter=lambda **_kwargs: FakeProcess()
        ) as broker:
            broker.request_approval(plan=plan)
            with self.assertRaises(LinkTranscriptionError) as replayed:
                broker.request_approval(plan=plan)
        self.assertEqual(replayed.exception.code, "APPROVAL_CONSUMED")

    def test_private_pipe_has_real_eof_and_timeout_boundaries(self) -> None:
        from src.core.cli_runtime.link_host_bridge import (
            LinkTranscriptionError,
            _PrivatePipeProcess,
        )

        payload = b"https://example.com/watch?secret=pipe-only"
        echo = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        pipe = _PrivatePipeProcess(echo, Path(sys.executable))
        self.assertEqual(
            pipe.exchange(payload, timeout=1.0, max_bytes=1024),
            payload,
        )

        stalled = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        timed = _PrivatePipeProcess(stalled, Path(sys.executable))
        with self.assertRaises(LinkTranscriptionError) as blocked:
            timed.exchange(payload, timeout=0.02, max_bytes=1024)
        self.assertEqual(blocked.exception.reason_code, "HOST_TIMEOUT")
        self.assertIsNotNone(stalled.poll())


if __name__ == "__main__":
    unittest.main()
