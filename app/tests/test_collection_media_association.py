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

    def test_same_id_on_three_platforms_and_same_title_never_collide(self):
        source = self.source()
        paths = {
            self.store.commit(
                source,
                stable_key=f"{platform}:42",
                download_mode="audio",
            ).relative_path
            for platform in ("douyin", "xiaohongshu", "bilibili")
        }
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(self.store.validate(path).valid for path in paths))

    def test_live_validation_rejects_missing_escape_symlink_directory_and_empty(self):
        root = self.store.root
        root.mkdir(parents=True)
        (root / "douyin").mkdir()
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"outside")
        (root / "douyin" / "link.m4a").symlink_to(outside)
        (root / "douyin" / "folder.m4a").mkdir()
        (root / "douyin" / "empty.m4a").write_bytes(b"")
        for value in (
            "douyin/missing.m4a",
            "../outside.m4a",
            "douyin/link.m4a",
            "douyin/folder.m4a",
            "douyin/empty.m4a",
        ):
            with self.subTest(value=value):
                self.assertFalse(self.store.validate(value).valid)

    def test_existing_root_below_symlink_ancestor_is_rejected_without_write(self):
        real_parent = self.root / "real-parent"
        managed_root = real_parent / "collection" / "media"
        managed_root.mkdir(parents=True)
        alias_parent = self.root / "alias-parent"
        alias_parent.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaises(OSError):
            ManagedMediaStore(alias_parent / "collection" / "media")

        self.assertEqual(list(managed_root.iterdir()), [])

    def test_cross_stable_key_media_is_identity_mismatch_and_never_reused(self):
        source = self.source()
        first = self.store.commit(
            source,
            stable_key="douyin:100",
            download_mode="audio",
        )
        self.assertEqual(first.status, "downloaded")
        mismatch = self.store.validate(
            first.relative_path,
            expected_stable_key="douyin:200",
        )
        self.assertFalse(mismatch.valid)
        self.assertEqual(mismatch.reason_code, "media_identity_mismatch")

        wrong_owner = item(
            "douyin",
            "200",
            media_local_path=first.relative_path,
            media_status="downloaded",
        )
        self.repo.upsert([wrong_owner])
        implicit = self.store.validate(first.relative_path)
        self.assertFalse(implicit.valid)
        self.assertEqual(implicit.reason_code, "media_identity_mismatch")
        fetch_calls = []

        def fetch(*_args):
            fetch_calls.append(1)
            return source

        result = CollectionMediaAssociationService(
            self.repo,
            self.store,
            fetch,
        ).run([wrong_owner], policy=self.policy(stable_key="douyin:200"))

        self.assertEqual(fetch_calls, [])
        self.assertEqual(result.download_calls, 0)
        self.assertEqual(result.outcomes[0].status, "failed")
        self.assertEqual(
            result.outcomes[0].reason_code,
            "media_identity_mismatch",
        )
        self.assertFalse(result.outcomes[0].reused)
        self.assertEqual(result.outcomes[0].relative_path, "")
        self.assertEqual(
            self.repo.load().items[0].media_local_path,
            first.relative_path,
        )

    def test_content_change_breaks_filename_receipt_binding(self):
        source = self.source(payload=b"original-audio")
        receipt = self.store.commit(
            source,
            stable_key="douyin:content-bound",
            download_mode="audio",
        )
        self.assertEqual(receipt.status, "downloaded")
        target = self.store.root.joinpath(
            *PurePosixPath(receipt.relative_path).parts
        )
        target.write_bytes(b"tampered-audio")

        checked = self.store.validate(
            receipt.relative_path,
            expected_stable_key="douyin:content-bound",
        )
        self.assertFalse(checked.valid)
        self.assertEqual(checked.reason_code, "media_identity_mismatch")

    def test_parent_directory_swap_during_publish_fails_without_residue(self):
        source = self.source()
        real_link = __import__("os").link
        platform_dir = self.store.root / "douyin"
        detached = self.store.root / "detached-douyin"
        swapped = False

        def swap_then_link(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                platform_dir.rename(detached)
                platform_dir.mkdir()
            return real_link(*args, **kwargs)

        with patch("src.core.collect.media.os.link", side_effect=swap_then_link):
            receipt = self.store.commit(
                source,
                stable_key="douyin:directory-swap",
                download_mode="audio",
            )

        self.assertEqual(receipt.reason_code, "media_commit_uncertain")
        self.assertEqual(list(platform_dir.iterdir()), [])
        self.assertEqual(list(detached.iterdir()), [])

    def test_disabled_policy_makes_zero_fetch_and_zero_repository_change(self):
        original = item("douyin", "1", title="kept")
        self.repo.upsert([original])
        calls = []
        service = CollectionMediaAssociationService(
            self.repo, self.store, lambda *_args: calls.append(1)
        )

        result = service.run([original], policy=self.policy(save=False))

        self.assertEqual(result.download_calls, 0)
        self.assertEqual(calls, [])
        self.assertEqual(self.repo.load().items[0].title, "kept")

    def test_fetch_reason_code_is_preserved_without_false_repository_success(self):
        original = item("douyin", "failed-source")
        self.repo.upsert([original])

        def fail(*_args):
            raise ManagedMediaFetchError(
                "media_source_refresh_failed",
                "播放地址已过期，作品页也未能刷新",
            )

        result = CollectionMediaAssociationService(
            self.repo,
            self.store,
            fail,
        ).run(
            [original],
            policy=self.policy(stable_key=original.stable_key),
        )

        saved = self.repo.load().items[0]
        self.assertEqual(result.outcomes[0].status, "failed")
        self.assertEqual(
            result.outcomes[0].reason_code,
            "media_source_refresh_failed",
        )
        self.assertEqual(saved.media_status, "not_requested")
        self.assertEqual(saved.media_local_path, "")
        self.assertEqual(list(self.store.root.glob("**/*")), [])

    def test_valid_work_page_remains_eligible_when_play_url_is_invalid(self):
        original = item(
            "douyin",
            "fallback",
            play_url="http://expired.example.test/video.mp4",
            work_url="https://www.douyin.com/video/fallback",
        )
        self.repo.upsert([original])
        source = self.source()
        calls = []

        def fetch(current, *_args):
            calls.append(current.stable_key)
            return source

        self.assertEqual(
            reliable_media_sources(original),
            ("https://www.douyin.com/video/fallback",),
        )
        result = CollectionMediaAssociationService(
            self.repo,
            self.store,
            fetch,
        ).run([original], policy=self.policy(stable_key=original.stable_key))

        self.assertEqual(calls, [original.stable_key])
        self.assertEqual(result.outcomes[0].status, "downloaded")
        self.assertTrue(
            self.store.validate(
                result.items[0].media_local_path,
                expected_stable_key=original.stable_key,
            ).valid
        )

    def test_media_sources_are_https_credential_free_ordered_and_deduplicated(self):
        original = item(
            "douyin",
            "sources",
            play_url="https://media.example.test/sources.mp4",
            work_url="https://www.douyin.com/video/sources",
        )
        self.assertEqual(
            reliable_media_sources(original),
            (
                "https://media.example.test/sources.mp4",
                "https://www.douyin.com/video/sources",
            ),
        )
        self.assertEqual(
            reliable_media_sources(replace(original, work_url=original.play_url)),
            (original.play_url,),
        )
        self.assertEqual(
            reliable_media_sources(
                replace(
                    original,
                    play_url="https://user:secret@media.example.test/video.mp4",
                    work_url="ftp://www.douyin.com/video/sources",
                )
            ),
            (),
        )

    def test_commit_and_field_cas_preserve_unrelated_newer_fields(self):
        original = item("douyin", "1", title="old")
        self.repo.upsert([original])
        source = self.source()

        def fetch(_item, _mode, _destination, _cancel):
            current = self.repo.load().items[0]
            newer = CollectedItem(**{**current.to_dict(), "title": "newer"})
            self.repo.upsert([newer])
            return source

        result = CollectionMediaAssociationService(
            self.repo, self.store, fetch
        ).run([original], policy=self.policy())

        saved = self.repo.load().items[0]
        self.assertEqual(result.outcomes[0].status, "downloaded")
        self.assertEqual(saved.title, "newer")
        self.assertEqual(saved.media_status, "downloaded")
        self.assertTrue(self.store.validate(saved.media_local_path).valid)

    def test_two_media_writers_conflict_without_overwriting_newer_truth(self):
        original = item("douyin", "1")
        self.repo.upsert([original])
        source = self.source()

        def competing_fetch(_item, _mode, _destination, _cancel):
            self.repo.compare_and_swap_fields(
                original.stable_key,
                expected={"media_local_path": "", "media_status": "not_requested"},
                updates={"media_local_path": "douyin/newer.m4a", "media_status": "downloaded"},
            )
            return source

        result = CollectionMediaAssociationService(
            self.repo, self.store, competing_fetch
        ).run([original], policy=self.policy())

        saved = self.repo.load().items[0]
        self.assertEqual(result.outcomes[0].reason_code, "field_conflict")
        self.assertEqual(saved.media_local_path, "douyin/newer.m4a")

    def test_cancel_and_disk_full_fail_closed(self):
        source = self.source()
        cancelled = self.store.commit(
            source,
            stable_key="douyin:1",
            download_mode="audio",
            is_cancelled=lambda: True,
        )
        self.assertEqual(cancelled.reason_code, "media_cancelled")
        with patch(
            "src.core.collect.media.shutil.copyfileobj",
            side_effect=OSError(errno.ENOSPC, "full"),
        ):
            full = self.store.commit(
                source,
                stable_key="douyin:2",
                download_mode="audio",
            )
        self.assertEqual(full.reason_code, "media_disk_full")

    def test_type_permission_and_source_identity_fail_closed(self):
        audio = self.source("audio.m4a")
        invalid = self.source("payload.txt")
        self.assertEqual(
            self.store.commit(
                audio, stable_key="douyin:1", download_mode="video"
            ).reason_code,
            "media_video_unavailable",
        )
        self.assertEqual(
            self.store.commit(
                invalid, stable_key="douyin:2", download_mode="audio"
            ).reason_code,
            "media_type_invalid",
        )
        with patch("src.core.collect.media.os.link", side_effect=PermissionError()):
            denied = self.store.commit(
                audio, stable_key="douyin:3", download_mode="audio"
            )
        self.assertEqual(denied.reason_code, "media_permission_denied")
        with patch.object(
            self.store,
            "_source_identity",
            side_effect=[(1, 2, 5, 10), (1, 2, 5, 11)],
        ):
            changed = self.store.commit(
                audio, stable_key="douyin:4", download_mode="audio"
            )
        self.assertEqual(changed.reason_code, "media_source_changed")

    def test_verified_success_reuses_and_missing_file_fetches_again(self):
        original = item("douyin", "1")
        self.repo.upsert([original])
        source = self.source()
        calls = []

        def fetch(*_args):
            calls.append(1)
            return source

        service = CollectionMediaAssociationService(self.repo, self.store, fetch)
        first = service.run([original], policy=self.policy())
        saved = self.repo.load().items[0]
        second = service.run([saved], policy=self.policy())
        self.assertEqual((first.download_calls, second.download_calls), (1, 0))
        self.assertEqual(len(calls), 1)
        checked = self.store.validate(
            saved.media_local_path,
            expected_stable_key=saved.stable_key,
        )
        self.assertTrue(checked.valid)
        checked.path.unlink()
        third = service.run([saved], policy=self.policy())
        self.assertEqual(third.download_calls, 1)
        self.assertEqual(len(calls), 2)

    def test_transcription_lookup_resolves_only_matching_verified_record(self):
        source = self.source(payload=b"managed-transcription-input")
        receipt = self.store.commit(
            source,
            stable_key="douyin:transcribe",
            download_mode="audio",
        )
        original = item(
            "douyin",
            "transcribe",
            media_status="downloaded",
            media_local_path=receipt.relative_path,
        )
        self.repo.upsert([original])

        resolved = resolve_managed_transcription_media(
            original.stable_key,
            repository=self.repo,
            store=self.store,
        )

        self.assertTrue(resolved.valid)
        self.assertEqual(resolved.path.read_bytes(), b"managed-transcription-input")

    def test_transcription_lookup_fails_closed_for_missing_and_tampered_media(self):
        missing = resolve_managed_transcription_media(
            "douyin:missing",
            repository=self.repo,
            store=self.store,
        )
        self.assertFalse(missing.valid)
        self.assertEqual(missing.reason_code, "media_record_missing")

        source = self.source(payload=b"original")
        receipt = self.store.commit(
            source,
            stable_key="douyin:tampered",
            download_mode="audio",
        )
        original = item(
            "douyin",
            "tampered",
            media_status="downloaded",
            media_local_path=receipt.relative_path,
        )
        self.repo.upsert([original])
        target = self.store.root.joinpath(
            *PurePosixPath(receipt.relative_path).parts
        )
        target.write_bytes(b"tampered")

        tampered = resolve_managed_transcription_media(
            original.stable_key,
            repository=self.repo,
            store=self.store,
        )
        self.assertFalse(tampered.valid)
        self.assertEqual(tampered.reason_code, "media_identity_mismatch")

    def test_transcription_lookup_repository_error_becomes_fallback_receipt(self):
        with patch.object(
            self.repo,
            "load",
            side_effect=RepositoryError("read_failed"),
        ):
            result = resolve_managed_transcription_media(
                "douyin:repository-error",
                repository=self.repo,
                store=self.store,
            )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_code, "media_lookup_unavailable")


if __name__ == "__main__":
    unittest.main()
