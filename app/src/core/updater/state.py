from __future__ import annotations

from pathlib import Path

from .constants import default_updates_root
from .models import UpdateError, UpdateSnapshot
from .secure_io import atomic_json, ensure_private_dir, load_json

_FIELDS = set(UpdateSnapshot.__dataclass_fields__)
_STATES = {"idle","checking","available","downloading","download_paused","downloaded","staged","ready_to_install","install_blocked","installing","waiting_ready","installed","rolled_back","failed","failed_security_rejected","failed_signature","failed_state_corrupt","recovering"}
_UI_STATES = {"idle","checking_auto","checking_manual","up_to_date","update_available","manual_required","downloading","verifying","preparing","ready","ready_blocked","download_paused","check_error","manifest_verify_error","download_error","verify_error","zip_structure_error","preparation_error","location_error","install_error_rollback","rollback_failed","old_version_after_restart","installed","install_interrupted"}


class StateStore:
    def __init__(self, root: Path | None = None):
        self.root = ensure_private_dir(root or default_updates_root())
        for name in ("downloads", "staged", "transactions", "logs"):
            ensure_private_dir(self.root / name)
        self.path = self.root / "state.json"

    def save(self, snapshot: UpdateSnapshot) -> None:
        atomic_json(self.path, snapshot.to_dict())

    def record_result(self, result: str, *, txn: str = '', error_code: str = '') -> None:
        try:
            snapshot = self.load()
        except UpdateError:
            snapshot = UpdateSnapshot()
        mapping = {'installed': ('installed', 'installed'), 'rolled_back': ('rolled_back', 'install_error_rollback')}
        snapshot.state, snapshot.ui_state = mapping.get(result, ('failed', 'rollback_failed'))
        if txn:
            snapshot.txn = txn
        snapshot.error_code = error_code or ('' if result in mapping else result)
        snapshot.message = ''
        self.save(snapshot)

    def load(self) -> UpdateSnapshot:
        if not self.path.exists():
            return UpdateSnapshot()
        value = load_json(self.path, allowed=_FIELDS)
        defaults = UpdateSnapshot()
        for key, item in value.items():
            expected = type(getattr(defaults, key))
            if expected is int and type(item) is not int:
                raise UpdateError("state_corrupt", "更新状态类型异常")
            if expected is bool and type(item) is not bool:
                raise UpdateError("state_corrupt", "更新状态类型异常")
            if expected is str and not isinstance(item, str):
                raise UpdateError("state_corrupt", "更新状态类型异常")
            if expected is dict and not isinstance(item, dict):
                raise UpdateError("state_corrupt", "更新状态类型异常")
        if value.get("state", "idle") not in _STATES or value.get("ui_state", "idle") not in _UI_STATES or value.get("source", "auto") not in {"auto","manual"}:
            raise UpdateError("state_corrupt", "更新状态枚举异常")
        return UpdateSnapshot(**value)
