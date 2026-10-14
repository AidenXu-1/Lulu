from __future__ import annotations

import ctypes
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class Generation9ConnectionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        from src.core.agent_entry.handshake import PeerRequirement
        from src.core.agent_entry.native_transport import NativePeerFactsV1

        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.cli = root / "lulu-agent-cli"
        self.gui = root / "Lulu"
        self.core = root / "lulu-agent-core"
        self.cli.write_bytes(b"cli\n")
        self.gui.write_bytes(b"gui\n")
        self.core.write_bytes(b"core\n")
        self.cli.chmod(0o700)
        self.gui.chmod(0o700)
        self.core.chmod(0o700)
        common = dict(
            user_id=os.getuid(),
            audit_session_id=100,
            product_version="0.3.1",
            product_build="20260815.3",
            source_revision="a" * 40,
            generation=9,
            manifest_sha256="b" * 64,
            catalog_sha256="c" * 64,
            protocol_version="1.0.0",
        )
        self.cli_requirement = PeerRequirement.from_file(role="agent_cli", path=self.cli, **common)
        self.gui_requirement = PeerRequirement.from_file(role="gui", path=self.gui, **common)
        self.core_requirement = PeerRequirement.from_file(role="agent_core", path=self.core, **common)

        def facts(path: Path, *, pid: int, instance: int):
            metadata = path.stat()
            value = NativePeerFactsV1()
            value.schema_version = 1
            value.pid = pid
            value.effective_uid = os.getuid()
            value.effective_gid = os.getgid()
            value.audit_uid = os.getuid()
            value.audit_session_id = 100
            value.pidversion = pid + 1
            value.device = metadata.st_dev
            value.inode = metadata.st_ino
            value.size = metadata.st_size
            value.mode = 0o700
            value.connection_instance = instance
            token = secrets.token_bytes(32)
            ctypes.memmove(value.audit_token, token, 32)
            value.executable_path = os.fsencode(path)
            return value

        self.client_facts = facts(self.cli, pid=111, instance=1001)
        self.gui_facts = facts(self.gui, pid=333, instance=3001)
        self.core_facts = facts(self.core, pid=222, instance=2001)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_workspace_lists_cross_measured_core_without_changing_service_values(self):
        """Exercise the installed adapter, protocol and real workspace service together."""
        from types import SimpleNamespace
        from src.core import library
        from src.core.agent_workspace import client as workspace_client
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient

        root = Path(self.temporary.name).resolve()
        (root / 'installation').mkdir(mode=0o700)
        dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=root / 'Resources',
                                           runtime_factory=lambda: object())
        installation = SimpleNamespace(paths=SimpleNamespace(state_root=root / 'installation'))
        protocol = AgentCoreProtocol(client_requirement=self.gui_requirement, dispatcher=dispatcher)

        class Exchange:
            def exchange(inner_self, payload, *, timeout_ms=5000):
                # In production the receiver is a separate frozen Core process.
                with patch.object(workspace_client.sys, 'executable', '/Applications/Lulu.app/Contents/MacOS/lulu-agent-core'):
                    return protocol.handle(payload, self.gui_facts), self.core_facts

        measured = MeasuredCoreClient(exchange=Exchange(), client_requirement=self.gui_requirement,
                                      core_requirement=self.core_requirement)
        with patch.object(library, 'LIBRARY_DIR', root / 'library'), \
             patch.object(library, 'FOLDERS_FILE', root / 'library/folders.json'), \
             patch('src.core.agent_entry.product_install.build_default_product_installer', return_value=installation), \
             patch('src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher', return_value=measured.dispatch):
            for operation in ('folders', 'presets', 'templates', 'list'):
                with self.subTest(operation=operation):
                    with patch.object(workspace_client.sys, 'frozen', False, create=True):
                        expected = workspace_client.dispatch(operation)
                    with patch.object(workspace_client.sys, 'frozen', True, create=True), \
                         patch.object(workspace_client.sys, 'executable', '/Applications/Lulu.app/Contents/MacOS/Lulu'):
                        actual = workspace_client.dispatch(operation)
                    self.assertEqual(actual, expected)

    def test_workspace_client_rejects_malformed_reply_without_replaying_request(self):
        from unittest.mock import Mock
        from src.core.agent_workspace import client as workspace_client

        for reply in ([], {}, {'unexpected': []}, {'result': [], 'unexpected': True}):
            transport = Mock(return_value=reply)
            with self.subTest(reply=reply), \
                 patch.object(workspace_client.sys, 'frozen', True, create=True), \
                 patch.object(workspace_client.sys, 'executable', '/Applications/Lulu.app/Contents/MacOS/Lulu'), \
                 patch('src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher', return_value=transport):
                with self.assertRaisesRegex(RuntimeError, '工作区回复格式无效'):
                    workspace_client.dispatch('folders')
                transport.assert_called_once()

    def test_h0_h1_h2_precede_business_and_replay_or_switch_is_rejected(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient

        effects: list[dict[str, object]] = []
        protocol = AgentCoreProtocol(
            client_requirement=self.cli_requirement,
            dispatcher=lambda request: effects.append(request) or {"exit_code": 0},
            challenge_factory=lambda: "d" * 64,
        )
        connection_id = "connection-" + "1" * 32

        with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED"):
            protocol.handle(
                {"phase": "BUSINESS", "connection_id": connection_id, "command": "doctor", "arguments": {}},
                self.client_facts,
            )
        self.assertEqual(effects, [])

        class Exchange:
            def __init__(inner_self):
                inner_self.phases: list[str] = []

            def exchange(inner_self, payload, *, timeout_ms=5000):
                del timeout_ms
                inner_self.phases.append(str(payload["phase"]))
                return protocol.handle(payload, self.client_facts), self.core_facts

        exchange = Exchange()
        client = MeasuredCoreClient(
            exchange=exchange,
            client_requirement=self.cli_requirement,
            core_requirement=self.core_requirement,
            connection_id=connection_id,
            challenge="e" * 64,
        )
        self.assertEqual(client.dispatch("doctor", {}), {"exit_code": 0})
        self.assertEqual(exchange.phases, ["H0", "H1", "H2", "BUSINESS"])
        self.assertEqual(effects, [{"command": "doctor", "arguments": {}}])

        switched = type(self.client_facts).from_buffer_copy(self.client_facts)
        switched.connection_instance += 1
        with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED") as caught:
            protocol.handle(
                {"phase": "BUSINESS", "connection_id": connection_id, "command": "doctor", "arguments": {}},
                switched,
            )
        self.assertEqual(caught.exception.effect_count, 0)
        self.assertEqual(len(effects), 1)

    def test_measured_initialization_cost_fits_business_budget_without_replay(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient
        from src.core.agent_entry.native_transport import XpcTransportError

        for cost_ms in (10120, 60001):
            with self.subTest(cost_ms=cost_ms):
                calls = []
                effects = []
                protocol = AgentCoreProtocol(
                    client_requirement=self.cli_requirement,
                    dispatcher=lambda request: effects.append(request) or {"exit_code": 0},
                    challenge_factory=lambda: "d" * 64,
                )

                class Exchange:
                    def exchange(inner_self, payload, *, timeout_ms=5000):
                        calls.append((payload["phase"], timeout_ms))
                        # Virtual elapsed time; no wall-clock sleep or system calls.
                        if payload["phase"] == "BUSINESS" and cost_ms > timeout_ms:
                            raise XpcTransportError(21)
                        return protocol.handle(payload, self.client_facts), self.core_facts

                client = MeasuredCoreClient(
                    exchange=Exchange(), client_requirement=self.cli_requirement,
                    core_requirement=self.core_requirement,
                )
                if cost_ms == 10120:
                    self.assertEqual(client.dispatch("capabilities", {}), {"exit_code": 0})
                    self.assertEqual(len(effects), 1)
                else:
                    with self.assertRaises(XpcTransportError) as caught:
                        client.dispatch("capabilities", {})
                    self.assertEqual(caught.exception.native_code, 21)
                self.assertEqual(calls, [("H0", 60000), ("H1", 5000), ("H2", 5000), ("BUSINESS", 60000)])

    def test_long_local_transcription_wait_is_bounded_and_never_replays(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient
        from src.core.agent_entry.native_transport import XpcTransportError
        for argv, cost_ms, budget, succeeds in (
                (['transcribe', 'run', '/isolated/public.wav'], 120000, 1800000, True),
                (['transcribe', 'run', '/isolated/public.wav'], 1800001, 1800000, False),
                (['transcribe', 'status'], 60001, 60000, False),
                (['jobs', 'show', '--job-id', 'job-public'], 60001, 60000, False)):
            with self.subTest(argv=argv, cost_ms=cost_ms):
                effects, calls = [], []
                protocol = AgentCoreProtocol(client_requirement=self.cli_requirement,
                    dispatcher=lambda request: effects.append(request) or {'exit_code': 0})
                class Exchange:
                    def exchange(inner, payload, *, timeout_ms=5000):
                        calls.append((payload['phase'], timeout_ms))
                        response = protocol.handle(payload, self.client_facts)
                        if payload['phase'] == 'BUSINESS' and cost_ms > timeout_ms:
                            raise XpcTransportError(21)
                        return response, self.core_facts
                client = MeasuredCoreClient(exchange=Exchange(),
                    client_requirement=self.cli_requirement, core_requirement=self.core_requirement)
                if succeeds:
                    self.assertEqual(client.dispatch('argv', {'argv': argv}), {'exit_code': 0})
                else:
                    with self.assertRaises(XpcTransportError): client.dispatch('argv', {'argv': argv})
                self.assertEqual(calls, [('H0',60000),('H1',5000),('H2',5000),('BUSINESS',budget)])
                self.assertEqual(len(effects), 1)
                self.assertTrue(client.business_dispatched)
                self.assertEqual(effects[0]['arguments']['argv'], argv)

    def test_cold_core_start_has_bounded_wait_without_replaying_business(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient
        from src.core.agent_entry.native_transport import XpcTransportError

        for startup_ms in (10000, 60001):
            with self.subTest(startup_ms=startup_ms):
                effects, calls = [], []
                protocol = AgentCoreProtocol(
                    client_requirement=self.cli_requirement,
                    dispatcher=lambda request: effects.append(request) or {"exit_code": 0},
                )

                class Exchange:
                    def exchange(inner_self, payload, *, timeout_ms=5000):
                        calls.append(payload["phase"])
                        if payload["phase"] == "H0" and startup_ms > timeout_ms:
                            raise XpcTransportError(21)
                        return protocol.handle(payload, self.client_facts), self.core_facts

                client = MeasuredCoreClient(exchange=Exchange(), client_requirement=self.cli_requirement,
                                            core_requirement=self.core_requirement)
                if startup_ms == 10000:
                    self.assertEqual(client.dispatch("doctor", {}), {"exit_code": 0})
                    self.assertEqual(calls, ["H0", "H1", "H2", "BUSINESS"])
                    self.assertEqual(len(effects), 1)
                else:
                    with self.assertRaises(XpcTransportError):
                        client.dispatch("doctor", {})
                    self.assertEqual(calls, ["H0"])
                    self.assertEqual(effects, [])

    def test_business_reply_still_rejects_changed_core_identity(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient

        protocol = AgentCoreProtocol(
            client_requirement=self.cli_requirement, dispatcher=lambda request: {"exit_code": 0},
        )
        changed = type(self.core_facts).from_buffer_copy(self.core_facts)
        changed.pidversion += 1

        class Exchange:
            def exchange(inner_self, payload, *, timeout_ms=5000):
                facts = changed if payload["phase"] == "BUSINESS" else self.core_facts
                return protocol.handle(payload, self.client_facts), facts

        client = MeasuredCoreClient(exchange=Exchange(), client_requirement=self.cli_requirement,
                                    core_requirement=self.core_requirement)
        with self.assertRaisesRegex(RuntimeError, "CORE_COMPONENT_MISMATCH"):
            client.dispatch("capabilities", {})

    def test_failed_initialization_is_not_cached_as_success(self) -> None:
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher

        runtime = object()
        factory = Mock(side_effect=[RuntimeError("ISOLATED_FAILURE"), runtime])
        installation = SimpleNamespace(paths=SimpleNamespace(state_root=Path(self.temporary.name)),
                                       verify_installed=Mock(), _all_targets=lambda: {})
        resource_root = Path(self.temporary.name) / "Lulu.app/Contents/Resources"
        resource_root.mkdir(parents=True)
        dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=resource_root,
                                           runtime_factory=factory)
        dispatcher._external_delivery = True
        with patch("src.core.agent_entry.product_transaction.product_state_lock", side_effect=lambda _: nullcontext()), \
             patch("src.core.agent_entry.product_transaction.assert_business_available"):
            with self.assertRaisesRegex(RuntimeError, "ISOLATED_FAILURE"):
                dispatcher._initialize_runtime(installation)
            self.assertFalse(dispatcher._runtime_loaded)
            self.assertIsNone(dispatcher._runtime)
            self.assertEqual(factory.call_count, 1)
            dispatcher._initialize_runtime(installation)
            dispatcher._initialize_runtime(installation)
        self.assertTrue(dispatcher._runtime_loaded)
        self.assertIs(dispatcher._runtime, runtime)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(installation.verify_installed.call_count, 1)

    def test_generation_protocol_and_role_drift_fail_before_effect(self) -> None:
        from dataclasses import replace
        from src.core.agent_entry.protocol import AgentCoreProtocol

        effects: list[object] = []
        protocol = AgentCoreProtocol(
            client_requirement=replace(self.cli_requirement, generation=8),
            dispatcher=lambda value: effects.append(value),
        )
        with self.assertRaisesRegex((RuntimeError, ValueError), "invalid peer requirement"):
            protocol.handle(
                {"phase": "H0", "connection_id": "connection-" + "2" * 32, "challenge": "f" * 64},
                self.client_facts,
            )
        self.assertEqual(effects, [])

    def test_cli_and_gui_are_independent_measured_clients_of_one_core(self) -> None:
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient

        effects: list[str] = []
        protocol = AgentCoreProtocol(
            client_requirement=(self.cli_requirement, self.gui_requirement),
            dispatcher=lambda request: effects.append(str(request["command"])) or {"ok": True},
            challenge_factory=lambda: "d" * 64,
        )

        class Exchange:
            def __init__(inner_self, facts):
                inner_self.facts = facts

            def exchange(inner_self, payload, *, timeout_ms=5000):
                del timeout_ms
                return protocol.handle(payload, inner_self.facts), self.core_facts

        cli = MeasuredCoreClient(
            exchange=Exchange(self.client_facts),
            client_requirement=self.cli_requirement,
            core_requirement=self.core_requirement,
            connection_id="connection-" + "3" * 32,
            challenge="e" * 64,
        )
        gui = MeasuredCoreClient(
            exchange=Exchange(self.gui_facts),
            client_requirement=self.gui_requirement,
            core_requirement=self.core_requirement,
            connection_id="connection-" + "4" * 32,
            challenge="f" * 64,
        )

        self.assertEqual(cli.dispatch("argv", {"argv": ["jobs", "list"]}), {"ok": True})
        self.assertEqual(gui.dispatch("gui.jobs.list", {}), {"ok": True})
        self.assertEqual(effects, ["argv", "gui.jobs.list"])

        arbitrary = type(self.gui_facts).from_buffer_copy(self.gui_facts)
        arbitrary.inode += 1
        with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED"):
            protocol.handle(
                {
                    "phase": "H0",
                    "connection_id": "connection-" + "5" * 32,
                    "challenge": "a" * 64,
                },
                arbitrary,
            )
        self.assertEqual(effects, ["argv", "gui.jobs.list"])

    def test_license_actions_require_the_measured_gui_not_cli(self):
        from src.core.agent_entry.protocol import AgentCoreProtocol, MeasuredCoreClient
        effects = []
        protocol = AgentCoreProtocol(client_requirement=(self.cli_requirement, self.gui_requirement),
            dispatcher=lambda request: effects.append(request) or {'allowed': False})
        outer = self
        class Exchange:
            def __init__(self, facts): self.facts = facts
            def exchange(self, payload, *, timeout_ms=5000):
                return protocol.handle(payload, self.facts), outer.core_facts
        cli = MeasuredCoreClient(exchange=Exchange(self.client_facts), client_requirement=self.cli_requirement,
            core_requirement=self.core_requirement)
        with self.assertRaisesRegex(RuntimeError, 'COMPONENT_EVIDENCE_REQUIRED'):
            cli.dispatch('gui.license', {'action': 'activate', 'license_code': 'synthetic'})
        for command in ('gui.library', 'gui.text'):
            with self.assertRaisesRegex(RuntimeError, 'COMPONENT_EVIDENCE_REQUIRED'):
                cli.dispatch(command, {})
        self.assertEqual(effects, [])
        gui = MeasuredCoreClient(exchange=Exchange(self.gui_facts), client_requirement=self.gui_requirement,
            core_requirement=self.core_requirement)
        self.assertEqual(gui.dispatch('gui.license', {'action': 'startup_status'}), {'allowed': False})
        self.assertEqual(effects, [{'command': 'gui.license', 'arguments': {'action': 'startup_status'}}])

    def test_gui_dispatcher_verifies_app_once_but_measures_each_connection(self) -> None:
        from src.core.agent_entry.entrypoints import GuiCoreDispatcher

        class Connection:
            def __enter__(inner_self):
                return inner_self

            def __exit__(inner_self, *_args):
                return None

        class Bridge:
            audit_session_id = 100

            def __init__(inner_self):
                inner_self.connections = 0

            def connect(inner_self, service_name):
                self.assertEqual(service_name, "com.lulu.transcribe.agent-core")
                inner_self.connections += 1
                return Connection()

        client_constructions: list[object] = []

        class Client:
            def __init__(inner_self, **kwargs):
                client_constructions.append(kwargs)

            def dispatch(inner_self, command, arguments):
                return {"command": command, "arguments": arguments}

        bridge = Bridge()
        with (
            patch(
                "src.core.agent_entry.entrypoints.load_gui_peer_requirements",
                return_value=(self.gui_requirement, self.core_requirement, {}),
            ) as load,
            patch("src.core.agent_entry.entrypoints.MeasuredCoreClient", Client),
        ):
            isolated_app = self.cli.parent / "isolated-v1.app"
            isolated_app.mkdir()
            dispatcher = GuiCoreDispatcher(app=isolated_app, bridge=bridge)
            self.assertEqual(dispatcher("gui.jobs.list", {}), {
                "command": "gui.jobs.list",
                "arguments": {},
            })
            self.assertEqual(dispatcher("gui.jobs.list", {}), {
                "command": "gui.jobs.list",
                "arguments": {},
            })

        load.assert_called_once_with(self.cli.parent / "isolated-v1.app", audit_session_id=100)
        self.assertEqual(bridge.connections, 2)
        self.assertEqual(len(client_constructions), 2)


if __name__ == "__main__":
    unittest.main()
