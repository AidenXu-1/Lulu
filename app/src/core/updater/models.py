from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Asset:
    kind: str
    filename: str
    url: str
    sha256: str
    size: int
    tree_manifest_sha256: str = ""


@dataclass(frozen=True)
class UpdateManifest:
    schema: int
    app: str
    channel: str
    latest_version: str
    latest_build: str
    min_supported_version: str
    key_id: str
    mandatory: bool
    published_at: str
    notes_md: str
    zip_asset: Asset
    dmg_asset: Asset


@dataclass
class UpdateSnapshot:
    state: str = "idle"
    ui_state: str = "idle"
    source: str = "auto"
    current_version: str = ""
    current_build: str = ""
    latest_version: str = ""
    latest_build: str = ""
    asset_size: int = 0
    published_at: str = ""
    notes: str = ""
    downloaded: int = 0
    total: int = 0
    error_code: str = ""
    message: str = ""
    manual_url: str = ""
    txn: str = ""
    last_checked_at: str = ""
    update_available: bool = False
    verified_manifest: bool = False
    deferred: bool = False
    staged_app: str = ""
    staged_manifest_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
