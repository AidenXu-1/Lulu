"""Write-ahead Agent entry transaction and deterministic recovery."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import re
from typing import Protocol


ACTIONS = ("install", "repair", "entry_update", "close", "rollback")
STATES = (
    "disabled",
    "installing",
    "ready",
    "update_required",
    "ownership_conflict",
    "rollback_complete",
    "rollback_incomplete",
)
EFFECT_NAMES = ("core_registration", "skill", "launcher")
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BINDING_FIELDS = frozenset(
    {
        "release_dmg_sha256",
        "component_manifest_sha256",
        "app_component_set_digest",
        "installation_id_digest",
        "candidate_generation",
    }
)


@dataclass(frozen=True, slots=True)
class EffectProbe:
    state: str

    def __post_init__(self) -> None:
        if self.state not in {"absent", "before", "after", "third_party", "drift"}:
            raise ValueError("unknown effect state")


class TransactionEffect(Protocol):
    name: str

    def probe(self) -> EffectProbe: ...

    def apply(self) -> None: ...

    def rollback(self) -> None: ...


class TransactionError(RuntimeError):
    def __init__(self, reason_code: str, state: str, *, effect_count: int) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.state = state
        self.effect_count = effect_count


class CrashInjected(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600, follow_symlinks=False)
    _fsync_directory(path.parent)


def _replace_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600, follow_symlinks=False)
    _fsync_directory(path.parent)


def validate_installation_binding(binding: Mapping[str, object]) -> dict[str, object]:
    value = dict(binding)
    if value.get('source_kind') == 'signed_app':
        fields = {'source_kind', 'source_app_sha256', 'component_manifest_sha256',
                  'app_component_set_digest', 'installation_id_digest', 'candidate_generation'}
        if (set(value) != fields or value['candidate_generation'] != 9
                or value['source_app_sha256'] != value['app_component_set_digest']
                or any(type(value[k]) is not str or not _SHA256.fullmatch(value[k])
                       for k in fields-{'source_kind', 'candidate_generation'})):
            raise ValueError('invalid signed-App installation binding')
        return value
    if (
        frozenset(value) != _BINDING_FIELDS
        or value.get("candidate_generation") != 9
        or any(
            type(value.get(field)) is not str
            or not _SHA256.fullmatch(str(value.get(field)))
            for field in _BINDING_FIELDS - {"candidate_generation"}
        )
    ):
        raise ValueError("invalid generation-9 installation binding")
    return value


class AgentEntryTransaction:
    _schema_version = "v1"
    _effect_names = EFFECT_NAMES
    _actions = ACTIONS

    def __init__(self, state_root: Path) -> None:
        self._root = Path(state_root)
        self._journals = self._root / "journals"
        self._receipts = self._root / "receipts"
        for directory in (self._root, self._journals, self._receipts):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700, follow_symlinks=False)

    def _transaction_root(self, transaction_id: str) -> Path:
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise ValueError("invalid transaction id")
        root = self._journals / transaction_id
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700, follow_symlinks=False)
        return root

    def _append(
        self,
        transaction_id: str,
        *,
        action: str,
        phase: str,
        initial_states: dict[str, str],
        effect_count: int,
        binding: dict[str, object],
    ) -> dict[str, object]:
        root = self._transaction_root(transaction_id)
        generation = len(list(root.glob("*.json"))) + 1
        payload: dict[str, object] = {
            "schema_id": f"lulu.agent-entry-transaction/{self._schema_version}",
            "transaction_id": transaction_id,
            "action": action,
            "generation": generation,
            "phase": phase,
            "initial_states": dict(initial_states),
            "effect_count": effect_count,
            "binding": dict(binding),
        }
        _write_new_json(root / f"{generation:04d}-{phase}.json", payload)
        return payload

    def _receipt(
        self,
        transaction_id: str,
        *,
        action: str,
        phase: str,
        state: str,
        effect_count: int,
        binding: dict[str, object],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_id": f"lulu.agent-entry-action-receipt/{self._schema_version}",
            "transaction_id": transaction_id,
            "action": action,
            "phase": phase,
            "state": state,
            "effect_count": effect_count,
            "binding": dict(binding),
        }
        path = self._receipts / f"{transaction_id}.json"
        if not path.exists():
            _write_new_json(path, payload)
        return payload

    def _current(
        self,
        transaction_id: str,
        *,
        action: str,
        phase: str,
        state: str,
        effect_count: int,
        binding: dict[str, object],
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_id": f"lulu.agent-entry-current-action/{self._schema_version}",
            "transaction_id": transaction_id,
            "action": action,
            "phase": phase,
            "state": state,
            "effect_count": effect_count,
            "binding": dict(binding),
        }
        _replace_json(self._root / "current.json", payload)
        return payload

    def _ordered_effects(self, effects: list[TransactionEffect]) -> list[TransactionEffect]:
        by_name = {effect.name: effect for effect in effects}
        if len(by_name) != len(effects) or tuple(by_name) != self._effect_names:
            raise ValueError("transaction effects must be the exact closed set in order")
        return effects

    def execute(
        self,
        action: str,
        effects: list[TransactionEffect],
        *,
        transaction_id: str,
        binding: Mapping[str, object],
        final_verifier: Callable[[], bool],
        crash_after: str | None = None,
    ) -> dict[str, object]:
        if action not in self._actions:
            raise ValueError("unknown Agent entry action")
        ordered = self._ordered_effects(effects)
        exact_binding = validate_installation_binding(binding)
        initial = {effect.name: effect.probe().state for effect in ordered}
        if any(state == "third_party" for state in initial.values()):
            self._current(
                transaction_id,
                action=action,
                phase="PRECHECK_REJECTED",
                state="ownership_conflict",
                effect_count=0,
                binding=exact_binding,
            )
            raise TransactionError(
                "AGENT_ENTRY_OWNERSHIP_CONFLICT", "ownership_conflict", effect_count=0
            )
        if any(state == "drift" for state in initial.values()):
            self._current(
                transaction_id,
                action=action,
                phase="PRECHECK_REJECTED",
                state="update_required",
                effect_count=0,
                binding=exact_binding,
            )
            raise TransactionError(
                "AGENT_ENTRY_COMPONENT_MISMATCH", "update_required", effect_count=0
            )
        self._append(
            transaction_id,
            action=action,
            phase="PREPARED",
            initial_states=initial,
            effect_count=0,
            binding=exact_binding,
        )
        self._current(
            transaction_id,
            action=action,
            phase="PREPARED",
            state="installing",
            effect_count=0,
            binding=exact_binding,
        )
        effect_count = 0
        reverse = action in {"close", "rollback"}
        iterable = list(reversed(ordered)) if reverse else ordered
        for effect in iterable:
            phase_name = effect.name.upper()
            self._append(
                transaction_id,
                action=action,
                phase=f"{phase_name}_INTENT",
                initial_states=initial,
                effect_count=effect_count,
                binding=exact_binding,
            )
            if crash_after == f"{effect.name}:intent":
                raise CrashInjected(crash_after)
            if reverse:
                if effect.probe().state == "after":
                    effect.rollback()
                    effect_count += 1
                expected = {"before", "absent"}
            else:
                before_apply = effect.probe().state
                effect.apply()
                if before_apply != "after":
                    effect_count += 1
                expected = {"after"}
            if crash_after == f"{effect.name}:effect":
                raise CrashInjected(crash_after)
            if effect.probe().state not in expected:
                raise TransactionError(
                    "AGENT_ENTRY_TRANSACTION_DRIFT", "rollback_incomplete", effect_count=effect_count
                )
            self._append(
                transaction_id,
                action=action,
                phase=f"{phase_name}_APPLIED",
                initial_states=initial,
                effect_count=effect_count,
                binding=exact_binding,
            )
            if crash_after == f"{effect.name}:applied":
                raise CrashInjected(crash_after)
        try:
            final_verified = final_verifier()
        except Exception as exc:
            self._current(
                transaction_id,
                action=action,
                phase="FINAL_REJECTED",
                state="rollback_incomplete",
                effect_count=effect_count,
                binding=exact_binding,
            )
            raise TransactionError(
                "AGENT_ENTRY_FINAL_VERIFICATION_FAILED",
                "rollback_incomplete",
                effect_count=effect_count,
            ) from exc
        if final_verified is not True:
            self._current(
                transaction_id,
                action=action,
                phase="FINAL_REJECTED",
                state="rollback_incomplete",
                effect_count=effect_count,
                binding=exact_binding,
            )
            raise TransactionError(
                "AGENT_ENTRY_FINAL_VERIFICATION_FAILED",
                "rollback_incomplete",
                effect_count=effect_count,
            )
        if crash_after == "ownership:effect":
            raise CrashInjected(crash_after)
        self._append(
            transaction_id,
            action=action,
            phase="FINAL_VERIFIED",
            initial_states=initial,
            effect_count=effect_count,
            binding=exact_binding,
        )
        self._append(
            transaction_id,
            action=action,
            phase="COMMITTED",
            initial_states=initial,
            effect_count=effect_count,
            binding=exact_binding,
        )
        state = (
            "disabled"
            if action == "close"
            else "rollback_complete"
            if action == "rollback"
            else "ready"
        )
        receipt = self._receipt(
            transaction_id,
            action=action,
            phase="COMMITTED",
            state=state,
            effect_count=effect_count,
            binding=exact_binding,
        )
        self._current(
            transaction_id,
            action=action,
            phase="COMMITTED",
            state=state,
            effect_count=effect_count,
            binding=exact_binding,
        )
        return receipt

    def recover(
        self,
        transaction_id: str,
        effects: list[TransactionEffect],
        *,
        rollback_verifier: Callable[[dict[str, object]], bool] | None = None,
    ) -> dict[str, object]:
        ordered = self._ordered_effects(effects)
        root = self._transaction_root(transaction_id)
        journals = sorted(root.glob("*.json"))
        if not journals:
            raise TransactionError("AGENT_ENTRY_RECOVERY_REQUIRED", "rollback_incomplete", effect_count=0)
        latest = json.loads(journals[-1].read_text(encoding="utf-8"))
        action = str(latest["action"])
        binding = validate_installation_binding(latest["binding"])
        if latest["phase"] == "COMMITTED":
            state = (
                "disabled"
                if action == "close"
                else "rollback_complete"
                if action == "rollback"
                else "ready"
            )
            receipt = self._receipt(
                transaction_id,
                action=action,
                phase="COMMITTED",
                state=state,
                effect_count=int(latest["effect_count"]),
                binding=binding,
            )
            self._current(
                transaction_id,
                action=action,
                phase="COMMITTED",
                state=state,
                effect_count=int(latest["effect_count"]),
                binding=binding,
            )
            return receipt
        live = {effect.name: effect.probe().state for effect in ordered}
        if any(state in {"drift", "third_party"} for state in live.values()):
            state = "ownership_conflict" if "third_party" in live.values() else "rollback_incomplete"
            receipt = self._receipt(
                transaction_id,
                action=action,
                phase="RECOVERY_PRESERVED",
                state=state,
                effect_count=0,
                binding=binding,
            )
            self._current(
                transaction_id,
                action=action,
                phase="RECOVERY_PRESERVED",
                state=state,
                effect_count=0,
                binding=binding,
            )
            return receipt
        rolled_back = 0
        initial_states = dict(latest["initial_states"])
        for effect in reversed(ordered):
            if initial_states.get(effect.name) == "after":
                if effect.probe().state != "after":
                    return self._current(
                        transaction_id,
                        action=action,
                        phase="RECOVERY_PRESERVED",
                        state="rollback_incomplete",
                        effect_count=rolled_back,
                        binding=binding,
                    )
                continue
            if effect.probe().state == "after":
                effect.rollback()
                rolled_back += 1
            if effect.probe().state not in {"before", "absent"}:
                receipt = self._receipt(
                    transaction_id,
                    action=action,
                    phase="RECOVERY_PRESERVED",
                    state="rollback_incomplete",
                    effect_count=rolled_back,
                    binding=binding,
                )
                self._current(
                    transaction_id,
                    action=action,
                    phase="RECOVERY_PRESERVED",
                    state="rollback_incomplete",
                    effect_count=rolled_back,
                    binding=binding,
                )
                return receipt
        try:
            if rollback_verifier is not None and rollback_verifier(binding) is not True:
                raise RuntimeError("rollback material verification failed")
        except Exception:
            self._append(
                transaction_id,
                action=action,
                phase="RECOVERY_PRESERVED",
                initial_states=dict(latest["initial_states"]),
                effect_count=rolled_back,
                binding=binding,
            )
            return self._current(
                transaction_id,
                action=action,
                phase="RECOVERY_PRESERVED",
                state="rollback_incomplete",
                effect_count=rolled_back,
                binding=binding,
            )
        self._append(
            transaction_id,
            action=action,
            phase="ROLLBACK_APPLIED",
            initial_states=dict(latest["initial_states"]),
            effect_count=rolled_back,
            binding=binding,
        )
        receipt = self._receipt(
            transaction_id,
            action=action,
            phase="ROLLBACK_APPLIED",
            state="rollback_complete",
            effect_count=rolled_back,
            binding=binding,
        )
        self._current(
            transaction_id,
            action=action,
            phase="ROLLBACK_APPLIED",
            state="rollback_complete",
            effect_count=rolled_back,
            binding=binding,
        )
        return receipt


class OptionalSkillTransaction(AgentEntryTransaction):
    """The optional pair uses a separate v2 action receipt; Core is untouched."""
    _schema_version = "v2"
    _effect_names = ("skill", "launcher")
    _actions = ("skill_install", "skill_update")
