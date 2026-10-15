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


if __name__ == "__main__":
    unittest.main()
