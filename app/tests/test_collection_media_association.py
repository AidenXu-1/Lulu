"""schema v2 主页受管媒体关联的纯本地测试。"""

from __future__ import annotations

from dataclasses import replace
import errno
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.collect.media import (  # noqa: E402
    CollectionMediaAssociationService,
    ManagedMediaFetchError,
    ManagedMediaStore,
    reliable_media_sources,
    resolve_managed_transcription_media,
)
from src.core.collect.models import CollectedItem  # noqa: E402
from src.core.collect.preferences import MaterialPreferenceSnapshot  # noqa: E402
from src.core.collect.repository import (  # noqa: E402
    CollectionRepository,
    RepositoryError,
)
from src.core.task import TaskExecutionPolicy  # noqa: E402


def item(platform: str, work_id: str, **changes) -> CollectedItem:
    payload = dict(
        aweme_id=work_id if platform == "douyin" else "",
        platform=platform,
        work_id=work_id,
        play_url=f"https://media.example.test/{platform}/{work_id}.m4a",
    )
    payload.update(changes)
    return CollectedItem(**payload)


class ManagedMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ManagedMediaStore(self.root / "collection" / "media")
        self.repo = CollectionRepository(self.root / "collection" / "records.json")

    def source(self, name="source.m4a", payload=b"audio") -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def policy(self, stable_key="douyin:1", *, save=True, mode="audio"):
        return TaskExecutionPolicy(
            origin="homepage_history_material",
            mode="materialize",
            stable_key=stable_key,
            material_snapshot=MaterialPreferenceSnapshot(mode, save, False),
            media_destination="managed_collection",
        )


if __name__ == "__main__":
    unittest.main()
