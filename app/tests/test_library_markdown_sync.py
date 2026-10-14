import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import library, obsidian
from src.core.models import Transcript


class LibraryMarkdownSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_library_dir = library.LIBRARY_DIR
        library.LIBRARY_DIR = self.root / "library"

    def tearDown(self):
        library.LIBRARY_DIR = self.old_library_dir
        self.tmp.cleanup()

    def test_save_creates_markdown_body_file(self):
        entry_id = library.save(
            Transcript(
                text="第一版文稿",
                segments=[],
                language="zh",
                engine="test",
                source="unit",
                title="同步文稿",
            ),
            "同步文稿",
        )

        md = library.markdown_path(entry_id)
        self.assertTrue(md.exists())
        self.assertEqual(md.parent.name, "docs")
        self.assertEqual(md.read_text(encoding="utf-8"), "第一版文稿")
        self.assertEqual(library.load_markdown(entry_id), "第一版文稿")

    def test_external_markdown_edit_refreshes_json_text(self):
        entry_id = library.save(
            Transcript(text="原文", segments=[], language="zh", engine="test", source="unit", title="外部编辑"),
            "外部编辑",
        )
        library.markdown_path(entry_id).write_text("外部改过的正文", encoding="utf-8")

        loaded = library.load(entry_id)
        self.assertEqual(loaded.text, "外部改过的正文")
        raw = library._load_raw(entry_id)
        self.assertEqual(raw["text"], "外部改过的正文")
        self.assertEqual(raw["char_count"], len("外部改过的正文"))

    def test_save_markdown_updates_entry_and_load_text(self):
        entry_id = library.save_rewrite("旧输出", "输出稿", source_title="源文稿")

        before = library._load_raw(entry_id)["revision"]
        library.save_markdown(entry_id, "新输出")

        raw = library._load_raw(entry_id)
        self.assertGreater(raw["revision"], before)
        self.assertEqual(raw["text"], "新输出")
        self.assertEqual(library.load_text(entry_id), "新输出")

    def test_delete_removes_only_managed_json_and_markdown(self):
        entry_id = library.save(
            Transcript(text="受管正文", segments=[], language="zh", engine="test", source="unit"),
            "受管正文",
        )
        entry_path = library.LIBRARY_DIR / f"{entry_id}.json"
        markdown = library.markdown_path(entry_id)

        library.delete(entry_id)

        self.assertFalse(entry_path.exists())
        self.assertFalse(markdown.exists())

    def test_delete_rejects_external_markdown_path_and_preserves_sentinel(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "外部路径",
        )
        sentinel = self.root / "external.md"
        sentinel.write_bytes(b"DO-NOT-DELETE")
        sentinel_inode = sentinel.stat().st_ino
        raw = library._load_raw(entry_id)
        raw["markdown_path"] = str(sentinel)
        library._write_raw(raw)

        with self.assertRaisesRegex(ValueError, "路径不安全"):
            library.delete(entry_id)

        self.assertEqual(sentinel.read_bytes(), b"DO-NOT-DELETE")
        self.assertEqual(sentinel.stat().st_ino, sentinel_inode)
        self.assertTrue((library.LIBRARY_DIR / f"{entry_id}.json").exists())

    def test_delete_rejects_parent_traversal_without_opening_external_target(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "路径遍历",
        )
        sentinel = self.root / f"{entry_id}.md"
        sentinel.write_bytes(b"TRAVERSAL-SENTINEL")
        sentinel_inode = sentinel.stat().st_ino
        raw = library._load_raw(entry_id)
        raw["markdown_path"] = f"docs/../../{entry_id}.md"
        library._write_raw(raw)

        with self.assertRaisesRegex(ValueError, "路径不安全"):
            library.delete(entry_id)

        self.assertEqual(sentinel.read_bytes(), b"TRAVERSAL-SENTINEL")
        self.assertEqual(sentinel.stat().st_ino, sentinel_inode)
        self.assertTrue((library.LIBRARY_DIR / f"{entry_id}.json").exists())

    def test_delete_rejects_symlinked_docs_ancestor_without_external_unlink(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "符号链接祖先",
        )
        docs = library.LIBRARY_DIR / "docs"
        managed_backup = library.LIBRARY_DIR / "docs-managed"
        docs.rename(managed_backup)
        external_docs = self.root / "external-docs"
        external_docs.mkdir()
        sentinel = external_docs / f"{entry_id}.md"
        sentinel.write_bytes(b"EXTERNAL")
        sentinel_inode = sentinel.stat().st_ino
        docs.symlink_to(external_docs, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "目录不安全"):
            library.delete(entry_id)

        self.assertEqual(sentinel.read_bytes(), b"EXTERNAL")
        self.assertEqual(sentinel.stat().st_ino, sentinel_inode)
        self.assertTrue((library.LIBRARY_DIR / f"{entry_id}.json").exists())

    def test_delete_rejects_hardlinked_markdown_and_preserves_both_names(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "硬链接正文",
        )
        markdown = library.markdown_path(entry_id)
        markdown.unlink()
        sentinel = self.root / "external-hardlink.md"
        sentinel.write_bytes(b"HARDLINK")
        os.link(sentinel, markdown)

        with self.assertRaisesRegex(ValueError, "文件不安全"):
            library.delete(entry_id)

        self.assertEqual(sentinel.read_bytes(), b"HARDLINK")
        self.assertTrue(markdown.exists())
        self.assertEqual(sentinel.stat().st_nlink, 2)
        self.assertTrue((library.LIBRARY_DIR / f"{entry_id}.json").exists())

    def test_delete_rejects_library_root_identity_drift_before_any_unlink(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "根目录漂移",
        )
        managed_root = library.LIBRARY_DIR
        held_root = self.root / "library-held"
        markdown = library.markdown_path(entry_id)
        markdown_bytes = markdown.read_bytes()
        markdown_inode = markdown.stat().st_ino
        json_path = managed_root / f"{entry_id}.json"
        json_bytes = json_path.read_bytes()
        json_inode = json_path.stat().st_ino
        original_json_load = library.json.load

        def drift_after_read(stream):
            value = original_json_load(stream)
            managed_root.rename(held_root)
            managed_root.mkdir()
            return value

        with (
            patch.object(library.json, "load", side_effect=drift_after_read),
            self.assertRaisesRegex(ValueError, "身份已变化"),
        ):
            library.delete(entry_id)

        held_markdown = held_root / "docs" / f"{entry_id}.md"
        held_json = held_root / f"{entry_id}.json"
        self.assertEqual(held_markdown.read_bytes(), markdown_bytes)
        self.assertEqual(held_markdown.stat().st_ino, markdown_inode)
        self.assertEqual(held_json.read_bytes(), json_bytes)
        self.assertEqual(held_json.stat().st_ino, json_inode)

    def test_delete_rolls_back_json_when_markdown_staging_fails(self):
        entry_id = library.save(
            Transcript(text="正文", segments=[], language="zh", engine="test", source="unit"),
            "成组回滚",
        )
        json_path = library.LIBRARY_DIR / f"{entry_id}.json"
        markdown = library.markdown_path(entry_id)
        json_bytes = json_path.read_bytes()
        markdown_bytes = markdown.read_bytes()
        real_rename = library.os.rename

        def fail_markdown_stage(src, dst, *args, **kwargs):
            if src == f"{entry_id}.md" and str(dst).startswith(f".delete-{entry_id}-"):
                raise OSError("fixture markdown stage failure")
            return real_rename(src, dst, *args, **kwargs)

        with (
            patch.object(library.os, "rename", side_effect=fail_markdown_stage),
            self.assertRaisesRegex(OSError, "fixture markdown stage failure"),
        ):
            library.delete(entry_id)

        self.assertEqual(json_path.read_bytes(), json_bytes)
        self.assertEqual(markdown.read_bytes(), markdown_bytes)
        self.assertEqual(list(library.LIBRARY_DIR.glob(".delete-*")), [])
        self.assertEqual(list(markdown.parent.glob(".delete-*")), [])

    def test_obsidian_note_prefers_existing_managed_cover_file(self):
        cover_root = self.root / "collection" / "covers"
        local = cover_root / "xiaohongshu/fixture.jpg"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"local-cover")
        transcript = Transcript(
            text="正文",
            segments=[],
            language="zh",
            engine="test",
            source="unit",
            title="本地封面",
            meta={
                "cover_platform": "xiaohongshu",
                "cover_work_id": "fixture",
                "cover_url": "https://assets.example.test/remote.jpg",
                "cover_status": "downloaded",
                "cover_local_path": "xiaohongshu/fixture.jpg",
            },
        )
        with patch("src.core.models_manager.app_data_dir", return_value=self.root):
            body = obsidian.note_body(transcript, "本地封面")
        self.assertIn(local.resolve().as_uri(), body)
        self.assertNotIn("https://assets.example.test/remote.jpg", body)

    def test_obsidian_note_rejects_managed_hardlink_and_keeps_remote_reference(self):
        cover_root = self.root / "collection" / "covers"
        platform_root = cover_root / "xiaohongshu"
        platform_root.mkdir(parents=True)
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        hardlink = platform_root / "hardlink.jpg"
        os.link(outside, hardlink)
        transcript = Transcript(
            text="正文",
            segments=[],
            language="zh",
            engine="test",
            source="unit",
            title="硬链接封面",
            meta={
                "cover_platform": "xiaohongshu",
                "cover_work_id": "hardlink",
                "cover_url": "https://assets.example.test/remote.jpg",
                "cover_status": "downloaded",
                "cover_local_path": "xiaohongshu/hardlink.jpg",
            },
        )
        with patch("src.core.models_manager.app_data_dir", return_value=self.root):
            body = obsidian.note_body(transcript, "硬链接封面")
        self.assertEqual(hardlink.stat().st_nlink, 2)
        self.assertNotIn(outside.resolve().as_uri(), body)
        self.assertIn("https://assets.example.test/remote.jpg", body)
