"""Explicit-root, no-follow paths for configuration migration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from .contracts import ConfigRoots, integrity_error


def _validate_no_follow(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise integrity_error("CONFIG_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("CONFIG_PATH_SYMLINK_FORBIDDEN")


def _validate_root(path: object) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise integrity_error("CONFIG_ROOT_INVALID")
    _validate_no_follow(path)
    return path


def _validate_owner_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise integrity_error("CONFIG_DIRECTORY_UNAVAILABLE") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise integrity_error("CONFIG_DIRECTORY_INVALID")


def _ensure_owner_directory(path: Path, *, boundary: Path) -> None:
    _validate_no_follow(path)
    if path == boundary:
        components = (path,)
    else:
        try:
            relative = path.relative_to(boundary)
        except ValueError as exc:
            raise integrity_error("CONFIG_PATH_OUTSIDE_ROOT") from exc
        components_list = [boundary]
        current = boundary
        for component in relative.parts:
            current = current / component
            components_list.append(current)
        components = tuple(components_list)
    for directory in components:
        if not directory.exists():
            try:
                directory.mkdir(mode=0o700)
            except OSError as exc:
                raise integrity_error("CONFIG_DIRECTORY_CREATE_FAILED") from exc
        _validate_owner_directory(directory)


@dataclass(frozen=True)
class ConfigPaths:
    roots: ConfigRoots
    legacy_config: Path
    canonical_config: Path
    journal_directory: Path
    backup_directory: Path

    @classmethod
    def from_roots(cls, roots: ConfigRoots) -> "ConfigPaths":
        if not isinstance(roots, ConfigRoots):
            raise integrity_error("CONFIG_ROOTS_INVALID")
        home_root = _validate_root(roots.home_root)
        application_support_root = _validate_root(
            roots.application_support_root
        )
        _validate_root(roots.resource_root)
        return cls(
            roots=roots,
            legacy_config=home_root / ".本地转录app" / "config.json",
            canonical_config=(
                application_support_root / "Lulu" / "config" / "config.json"
            ),
            journal_directory=(
                application_support_root / "Lulu" / "config" / "migration"
            ),
            backup_directory=(
                application_support_root / "Lulu" / "config" / "backups"
            ),
        )

    def prepare_layout(self) -> None:
        _ensure_owner_directory(
            self.roots.home_root,
            boundary=self.roots.home_root,
        )
        _ensure_owner_directory(
            self.roots.application_support_root,
            boundary=self.roots.application_support_root,
        )
        _ensure_owner_directory(
            self.roots.resource_root,
            boundary=self.roots.resource_root,
        )
        for directory in (
            self.canonical_config.parent,
            self.journal_directory,
            self.backup_directory,
        ):
            _ensure_owner_directory(
                directory,
                boundary=self.roots.application_support_root,
            )

    @staticmethod
    def _safe_identity(value: object, reason_code: str) -> str:
        if type(value) is not str or not value or len(value) > 16 * 1024:
            raise integrity_error(reason_code)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def journal_path(self, migration_id: str) -> Path:
        digest = self._safe_identity(
            migration_id,
            "CONFIG_MIGRATION_ID_INVALID",
        )
        return self.journal_directory / f"migration-{digest}.json"

    def backup_path(self, migration_id: str, content_sha256: str) -> Path:
        identity = self._safe_identity(
            f"{migration_id}:{content_sha256}",
            "CONFIG_BACKUP_IDENTITY_INVALID",
        )
        return self.backup_directory / f"config-{identity}.json"
