"""Read-only projection of Lulu's one shared product storage layout."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat


class StorageLayoutError(RuntimeError):
    """The explicitly supplied product storage root is not trustworthy."""


def _validate_explicit_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise StorageLayoutError("Application Support根必须显式为绝对Path")
    current = Path(root.anchor)
    try:
        for component in root.parts[1:]:
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise StorageLayoutError("Application Support根祖先禁止符号链接")
        metadata = root.lstat()
    except StorageLayoutError:
        raise
    except OSError as exc:
        raise StorageLayoutError("Application Support根不可用") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise StorageLayoutError("Application Support根owner或mode无效")
    return root


@dataclass(frozen=True)
class SharedProductStorageLayout:
    application_support_root: Path
    product_root: Path
    config: Path
    jobs: Path
    models: Path
    library: Path
    voices: Path
    runtime: Path

    @classmethod
    def from_application_support_root(
        cls,
        application_support_root: Path,
    ) -> "SharedProductStorageLayout":
        root = _validate_explicit_root(application_support_root)
        product_root = root / "Lulu"
        return cls(
            application_support_root=root,
            product_root=product_root,
            config=product_root / "config",
            jobs=product_root / "jobs",
            models=product_root / "models",
            library=product_root / "library",
            voices=product_root / "voices",
            runtime=product_root / "runtime",
        )

    def project(self, adapter_id: str) -> dict[str, object]:
        if adapter_id not in {"gui", "agent_cli"}:
            raise StorageLayoutError("产品适配器不存在")
        return {
            "schema_id": "lulu.shared-storage/v1",
            "adapter_id": adapter_id,
            "paths": {
                "product_root": str(self.product_root),
                "config": str(self.config),
                "jobs": str(self.jobs),
                "models": str(self.models),
                "library": str(self.library),
                "voices": str(self.voices),
                "runtime": str(self.runtime),
            },
        }
