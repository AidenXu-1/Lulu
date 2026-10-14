"""Core-only read-only directory classification; candidates grant no authority."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from .contracts import integrity_error
from .store import (_LedgerReadProjection, _validate_no_follow_ancestors,
                    _validate_owned_mode, MAX_SNAPSHOT_BYTES, verify_existing_ledger)
from ..agent_entry.delivery_identity import strict_json
from ..license.store import SecureStoreError
from ..cli_runtime.contracts import CliRuntimeTrustError


def _json_object(raw, reason):
    try:
        result = strict_json(raw)
        if type(result) is not dict: raise ValueError("object required")
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise integrity_error(reason) from exc


@dataclass(frozen=True)
class TaskStorageInventory:
    canonical_state: str
    candidate_scopes: tuple[str, ...] = ()
    wrong_empty: bool = False
    legacy_empty: bool = False


def _directory(path):
    _validate_no_follow_ancestors(path)
    if not os.path.lexists(path): return None
    _validate_owned_mode(path, mode=0o700, directory=True)
    return frozenset(p.name for p in path.iterdir())


def inspect_task_storage(product_root: Path) -> TaskStorageInventory:
    """Never creates files, recovers journals, reads keys, or declares freshness.

    Core must combine this inventory with secure recovery registration and fully
    authenticate any candidate scope before binding or opening a writable store.
    """
    if not isinstance(product_root, Path) or not product_root.is_absolute():
        raise integrity_error("JOB_DATA_ROOT_MUST_BE_EXPLICIT_ABSOLUTE")
    _validate_no_follow_ancestors(product_root)
    if os.path.lexists(product_root):
        # Existing product installer creates intermediate Lulu with 0755. This
        # shared container holds no ledger authority; jobs and every ledger file
        # remain strictly 0700/0600. Never chmod an existing user's container.
        metadata = product_root.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o700, 0o755}):
            raise integrity_error("JOB_PRODUCT_CONTAINER_INVALID")
    canonical = _directory(product_root / "jobs")
    wrong_parent = _directory(product_root / "Lulu")
    if wrong_parent is not None and wrong_parent - {"jobs"}:
        raise integrity_error("JOB_WRONG_ROOT_HAS_CONTENT")
    wrong = _directory(product_root / "Lulu/jobs")
    if wrong or (wrong is not None and canonical is None):
        raise integrity_error("JOB_WRONG_ROOT_REQUIRES_RECOVERY")
    legacy_empty = False
    old_queue = product_root / "task-queue"
    old_names = _directory(old_queue)
    if old_names is not None:
        if old_names - {"pending.json"}:
            raise integrity_error("JOB_LEGACY_QUEUE_REQUIRES_RECOVERY")
        if "pending.json" in old_names:
            payload = _json_object(_LedgerReadProjection()._read_file(old_queue / "pending.json", 1024 * 1024),
                "JOB_LEGACY_QUEUE_REQUIRES_RECOVERY")
            if (set(payload) != {"schema", "tasks", "groups"}
                or type(payload["schema"]) is not int or payload["schema"] not in {1, 2}
                or payload["tasks"] != [] or payload["groups"] != []):
                raise integrity_error("JOB_LEGACY_QUEUE_REQUIRES_RECOVERY")
            legacy_empty = True
    if canonical is None:
        if legacy_empty or wrong_parent is not None:
            raise integrity_error("JOB_STORAGE_RECOVERY_REQUIRED")
        return TaskStorageInventory("absent")
    if not canonical:
        return TaskStorageInventory("empty", wrong_empty=wrong is not None, legacy_empty=legacy_empty)
    from ..gui_task_view import GuiTaskViewStore, VIEW_FILENAME
    if "job-ledger-v1" not in canonical or canonical - {"job-ledger-v1", "results", VIEW_FILENAME}:
        raise integrity_error("JOB_STORAGE_UNKNOWN_CONTENT")
    if VIEW_FILENAME in canonical:
        # Core itself writes this display projection beside the ledger. It may
        # survive restart, but never authenticates jobs or authorizes execution.
        try:
            GuiTaskViewStore(product_root / "jobs").load()
        except (RuntimeError, ValueError, OSError) as exc:
            raise integrity_error("JOB_STORAGE_UNKNOWN_CONTENT") from exc
    if "results" in canonical:
        from ..transcription_result_store import inspect_existing_results, TranscriptionResultStoreError
        try:
            inspect_existing_results(product_root / "jobs/results")
        except (TranscriptionResultStoreError, OSError) as exc:
            raise integrity_error("JOB_STORAGE_UNKNOWN_CONTENT") from exc
    directory = product_root / "jobs/job-ledger-v1"
    names = _directory(directory)
    if names is not None and "journal.pending" in names:
        raise integrity_error("JOB_LEDGER_RECOVERY_REQUIRED")
    if names != {"journal.jsonl", "snapshot.json", "transaction.lock"}:
        raise integrity_error("JOB_STORAGE_INCOMPLETE")
    for name in names:
        _validate_owned_mode(directory / name, mode=0o600, directory=False)
    payload = _json_object(_LedgerReadProjection()._read_file(directory / "snapshot.json", MAX_SNAPSHOT_BYTES),
        "JOB_LEDGER_SNAPSHOT_INVALID")
    jobs = payload.get("jobs")
    if type(jobs) is not list:
        raise integrity_error("JOB_LEDGER_SNAPSHOT_INVALID")
    candidates = set()
    for job in jobs:
        scope = job.get("installation_id") if type(job) is dict else None
        if type(scope) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", scope):
            raise integrity_error("JOB_LEDGER_SCOPE_CANDIDATE_INVALID")
        candidates.add(scope)
        if len(candidates) > 32:
            raise integrity_error("JOB_LEDGER_SCOPE_CANDIDATE_LIMIT")
    return TaskStorageInventory("complete", tuple(sorted(candidates)), wrong is not None, legacy_empty)


def authenticate_legacy_scope(product_root, inventory, anchor_for_scope):
    """Only Core may invoke the secure anchor boundary; never writes or binds.

    Job installation IDs are lookup hints. All candidates must be examined and
    exactly one must authenticate the complete unchanged authority.
    """
    if inventory != inspect_task_storage(product_root) or inventory.canonical_state != "complete":
        raise integrity_error("JOB_STORAGE_INVENTORY_CHANGED")
    accepted = []
    for scope in inventory.candidate_scopes:
        try:
            anchor = anchor_for_scope(scope)
            verify_existing_ledger(product_root / "jobs", anchor)
        except SecureStoreError as exc:
            raise integrity_error("JOB_SCOPE_SECURE_STORAGE_UNAVAILABLE") from exc
        except CliRuntimeTrustError as exc:
            if exc.reason_code not in {"JOB_HMAC_KEY_MISSING", "JOB_HMAC_KEY_INVALID", "JOB_HIGH_WATER_INVALID"}:
                raise integrity_error("JOB_SCOPE_SECURE_STORAGE_UNAVAILABLE") from exc
            continue
        except Exception:
            continue
        accepted.append((scope, anchor))
    if len(accepted) != 1:
        raise integrity_error("JOB_SCOPE_AMBIGUOUS" if accepted else "JOB_SCOPE_UNVERIFIED")
    if inspect_task_storage(product_root) != inventory:
        raise integrity_error("JOB_STORAGE_INVENTORY_CHANGED")
    scope, anchor = accepted[0]
    verify_existing_ledger(product_root / "jobs", anchor)
    return scope, anchor
