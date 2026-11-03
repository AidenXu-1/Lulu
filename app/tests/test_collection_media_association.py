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

    def service(self, *, error=None):
        self.calls = []
        def fetch(current, mode, temporary, cancel):
            self.calls.append(current.stable_key)
            if error:
                raise error
            target = temporary / "fixture.mp4"
            target.write_bytes(b"local synthetic media")
            return target
        return CollectionMediaAssociationService(self.repo, self.store, fetch)

    def test_commit_reopen_and_reuse_do_not_download_again(self):
        current = item("douyin", "1", work_url="https://www.douyin.com/video/1")
        self.repo.upsert([current])
        service = self.service()
        first = service.run([current], policy=self.policy(mode="video"))
        second = service.run([current], policy=self.policy(mode="video"))
        self.assertEqual(first.outcomes[0].status, "downloaded")
        self.assertTrue(second.outcomes[0].reused)
        self.assertEqual(self.calls, ["douyin:1"])
        saved = CollectionRepository(self.repo.path).load().items[0]
        valid = self.store.validate(saved.media_local_path, expected_stable_key=saved.stable_key)
        self.assertTrue(valid.valid)

    def test_cancel_never_downloads_or_changes_saved_record(self):
        current = item("douyin", "1")
        self.repo.upsert([current])
        before = self.repo.path.read_bytes()
        service = self.service()
        result = service.run([current], policy=self.policy(), is_cancelled=lambda: True)
        self.assertEqual(result.outcomes[0].status, "cancelled")
        self.assertEqual(self.calls, [])
        self.assertEqual(before, self.repo.path.read_bytes())




if __name__ == "__main__":
    unittest.main()
