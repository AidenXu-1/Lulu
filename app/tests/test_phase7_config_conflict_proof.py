"""Phase 7 platform human-presence proof binding for config conflicts."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


WARM_CONFIG = b'{"theme":"warm","enable_link_download":false}\n'
DARK_CONFIG = b'{"enable_link_download":false,"theme":"dark"}\n'
USE_LEGACY_DIGEST = (
    "5df3471f0c46363d6c00d79eac638b6e38d2fca5c10617451ad0bec12995570c"
)


class _StaticVerifier:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims
        self.calls = 0

    def verify(self, handle_ref: str) -> dict[str, object]:
        self.calls += 1
        self.last_handle_ref = handle_ref
        return dict(self.claims)


def _proof_claims(digest: str, *, replay_slot: str) -> dict[str, object]:
    return {
        "proof_version": "HUMAN_PRESENCE_PROOF_V1",
        "issuer_id": "platform-issuer",
        "issuer_verification_key_id": "platform-key-v1",
        "audience": "com.lulu.transcribe.core/approval/v1",
        "local_uid": 501,
        "installation_id": "install-001",
        "nonce": "nonce-config-001",
        "approval_id": "approval-config-001",
        "intent_payload_digest": digest,
        "issued_at": 1_776_864_000.0,
        "expires_at": 1_776_864_120.0,
        "monotonic_issued_at": 42_000.0,
        "monotonic_expires_at": 42_120.0,
        "replay_slot": replay_slot,
    }


class Phase7ConfigConflictProofTests(unittest.TestCase):
    def test_only_current_platform_proof_can_resolve_exact_bound_conflict(self) -> None:
        authority = importlib.import_module("src.core.agent_trust.authority")
        replay_module = importlib.import_module("src.core.agent_trust.replay")
        trust_contracts = importlib.import_module("src.core.agent_trust.contracts")
        contracts = importlib.import_module("src.core.config_migration.contracts")

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary).resolve())
            service = fixture["service"]
            config_paths = fixture["paths"]
            cas_store = fixture["cas_store"]
            owner = fixture["owner"]
            digest = service.conflict_intent_digest(
                migration_id="migration-conflict",
                decision=contracts.ConflictDecision.USE_LEGACY,
            )
            self.assertEqual(digest, USE_LEGACY_DIGEST)
            replay = replay_module.MemoryReplayStore()
            before = self._state(config_paths, cas_store, replay)

            with self.assertRaises(trust_contracts.AgentTrustError) as unavailable:
                service.resolve_conflict(
                    owner=owner,
                    migration_id="migration-conflict",
                    started_at="2026-08-20T08:10:00Z",
                    decision=contracts.ConflictDecision.USE_LEGACY,
                    proof_handle_ref="opaque-proof",
                    verifier=authority.UnavailablePlatformHumanPresenceVerifier(),
                    replay_store=replay,
                    pinned_issuers={
                        "platform-issuer": frozenset({"platform-key-v1"})
                    },
                    approval_id="approval-config-001",
                    local_uid=501,
                    installation_id="install-001",
                    nonce="nonce-config-001",
                    now_wall=1_776_864_060.0,
                    now_monotonic=42_060.0,
                )
            self.assertEqual(unavailable.exception.code, "CAPABILITY_UNAVAILABLE")
            self.assertEqual(self._state(config_paths, cas_store, replay), before)

            with self.assertRaises(TypeError):
                service.resolve_conflict(
                    owner=owner,
                    migration_id="migration-conflict",
                    started_at="2026-08-20T08:10:00Z",
                    decision=contracts.ConflictDecision.USE_LEGACY,
                    proof_handle_ref="opaque-proof",
                    verifier=_proof_claims(digest, replay_slot="plain-dict"),
                    replay_store=replay,
                    pinned_issuers={
                        "platform-issuer": frozenset({"platform-key-v1"})
                    },
                    approval_id="approval-config-001",
                    local_uid=501,
                    installation_id="install-001",
                    nonce="nonce-config-001",
                    now_wall=1_776_864_060.0,
                    now_monotonic=42_060.0,
                )
            self.assertEqual(self._state(config_paths, cas_store, replay), before)

            tampered_decision_verifier = _StaticVerifier(
                _proof_claims(digest, replay_slot="tampered-decision")
            )
            with self.assertRaises(trust_contracts.AgentTrustError) as tampered:
                service.resolve_conflict(
                    owner=owner,
                    migration_id="migration-conflict",
                    started_at="2026-08-20T08:10:00Z",
                    decision=contracts.ConflictDecision.USE_CANONICAL,
                    proof_handle_ref="opaque-proof-tampered",
                    verifier=tampered_decision_verifier,
                    replay_store=replay,
                    pinned_issuers={
                        "platform-issuer": frozenset({"platform-key-v1"})
                    },
                    approval_id="approval-config-001",
                    local_uid=501,
                    installation_id="install-001",
                    nonce="nonce-config-001",
                    now_wall=1_776_864_060.0,
                    now_monotonic=42_060.0,
                )
            self.assertEqual(tampered.exception.code, "INTEGRITY_ERROR")
            self.assertEqual(self._state(config_paths, cas_store, replay), before)

            verifier = _StaticVerifier(_proof_claims(digest, replay_slot="valid-slot"))
            receipt = service.resolve_conflict(
                owner=owner,
                migration_id="migration-conflict",
                started_at="2026-08-20T08:10:00Z",
                decision=contracts.ConflictDecision.USE_LEGACY,
                proof_handle_ref="opaque-proof-valid",
                verifier=verifier,
                replay_store=replay,
                pinned_issuers={
                    "platform-issuer": frozenset({"platform-key-v1"})
                },
                approval_id="approval-config-001",
                local_uid=501,
                installation_id="install-001",
                nonce="nonce-config-001",
                now_wall=1_776_864_060.0,
                now_monotonic=42_060.0,
            )
            self.assertEqual(receipt.outcome, "conflict_use_legacy")
            self.assertEqual(config_paths.canonical_config.read_bytes(), WARM_CONFIG)
            self.assertEqual(config_paths.legacy_config.read_bytes(), WARM_CONFIG)
            self.assertEqual(Path(receipt.backup_path).read_bytes(), DARK_CONFIG)
            self.assertEqual(replay.mutation_count, 1)
            journal = __import__("json").loads(
                Path(receipt.journal_path).read_text("utf-8")
            )
            self.assertEqual(
                journal["schema"],
                "lulu.config-migration-journal/v1",
            )
            self.assertEqual(journal["journal"]["state"], "committed")
            self.assertEqual(journal["journal"]["decision"], "use_legacy")
            self.assertEqual(
                journal["journal"]["backup_sha256"],
                "9e869a0cbc1197ff654dbc920388f9c62f9259d50bb6bd3708e982646fa2a8d2",
            )

        with TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary).resolve())
            service = fixture["service"]
            config_paths = fixture["paths"]
            cas_store = fixture["cas_store"]
            owner = fixture["owner"]
            replay = replay_module.MemoryReplayStore()
            decision = contracts.ConflictDecision.EXPORT_BOTH_AND_ABORT
            digest = service.conflict_intent_digest(
                migration_id="migration-export",
                decision=decision,
            )
            arguments = {
                "owner": owner,
                "migration_id": "migration-export",
                "started_at": "2026-08-20T08:11:00Z",
                "decision": decision,
                "proof_handle_ref": "opaque-proof-export",
                "verifier": _StaticVerifier(
                    _proof_claims(digest, replay_slot="export-slot")
                ),
                "replay_store": replay,
                "pinned_issuers": {
                    "platform-issuer": frozenset({"platform-key-v1"})
                },
                "approval_id": "approval-config-001",
                "local_uid": 501,
                "installation_id": "install-001",
                "nonce": "nonce-config-001",
                "now_wall": 1_776_864_060.0,
                "now_monotonic": 42_060.0,
            }
            exported = service.resolve_conflict(**arguments)
            self.assertEqual(exported.outcome, "conflict_exported_both_aborted")
            self.assertEqual(config_paths.legacy_config.read_bytes(), WARM_CONFIG)
            self.assertEqual(config_paths.canonical_config.read_bytes(), DARK_CONFIG)
            export_journal = __import__("json").loads(
                Path(exported.journal_path).read_text("utf-8")
            )
            self.assertEqual(export_journal["journal"]["decision"], decision.value)
            self.assertEqual(export_journal["journal"]["state"], "committed")
            stable = self._state(config_paths, cas_store, replay)
            with self.assertRaises(trust_contracts.AgentTrustError) as replayed:
                service.resolve_conflict(**arguments)
            self.assertEqual(replayed.exception.code, "APPROVAL_CONSUMED")
            self.assertEqual(self._state(config_paths, cas_store, replay), stable)

    @staticmethod
    def _fixture(temporary_root: Path) -> dict[str, object]:
        contracts = importlib.import_module("src.core.config_migration.contracts")
        paths_module = importlib.import_module("src.core.config_migration.paths")
        repository_module = importlib.import_module(
            "src.core.config_migration.repository"
        )
        migration_module = importlib.import_module(
            "src.core.config_migration.migration"
        )
        resource_contracts = importlib.import_module(
            "src.core.resource_control.contracts"
        )
        locks_module = importlib.import_module("src.core.resource_control.locks")
        cas_module = importlib.import_module("src.core.resource_control.cas")
        roots = contracts.ConfigRoots(
            home_root=temporary_root / "home",
            application_support_root=temporary_root / "support",
            resource_root=temporary_root / "resources",
        )
        config_paths = paths_module.ConfigPaths.from_roots(roots)
        config_paths.prepare_layout()
        config_paths.legacy_config.parent.mkdir(mode=0o700)
        config_paths.legacy_config.write_bytes(WARM_CONFIG)
        config_paths.canonical_config.write_bytes(DARK_CONFIG)
        os.chmod(config_paths.legacy_config, 0o600)
        os.chmod(config_paths.canonical_config, 0o600)
        lock_manager = locks_module.ResourceLockManager(roots.resource_root)
        cas_store = cas_module.ResourceCASStore(roots.resource_root, lock_manager)
        repository = repository_module.ConfigRepository(
            config_paths,
            lock_manager=lock_manager,
            cas_store=cas_store,
        )
        service = migration_module.ConfigMigrationService(
            config_paths,
            repository=repository,
            lock_manager=lock_manager,
        )
        return {
            "paths": config_paths,
            "cas_store": cas_store,
            "service": service,
            "owner": resource_contracts.LockOwner(
                pid=702,
                process_start="fake-start:702",
                installation_id="install-001",
                operation_id="config-conflict",
            ),
        }

    @staticmethod
    def _state(config_paths, cas_store, replay) -> tuple[object, ...]:
        def directory_state(path: Path) -> tuple[tuple[str, bytes], ...]:
            return tuple(sorted((item.name, item.read_bytes()) for item in path.iterdir()))

        return (
            config_paths.legacy_config.read_bytes(),
            config_paths.canonical_config.read_bytes(),
            directory_state(config_paths.journal_directory),
            directory_state(config_paths.backup_directory),
            directory_state(cas_store.record_directory),
            replay.snapshot(),
        )


if __name__ == "__main__":
    unittest.main()
