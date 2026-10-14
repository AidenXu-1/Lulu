import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src.core import library


class LibraryTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("LIBRARY_DIR", root), ("FOLDERS_FILE", root / "folders.json")):
            p = patch.object(library, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_simultaneous_stale_writes_have_one_winner(self):
        eid = library.save_document("原文", "并发")
        version = library.document_snapshot(eid)
        gate = threading.Barrier(2)

        def update(text):
            gate.wait(timeout=3)
            try:
                library.save_markdown(eid, text, expected_revision=version["revision"],
                                      expected_hash=version["content_hash"])
                return text
            except library.RevisionConflict:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(update, ["GUI 修改", "CLI 修改"]))
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(library.load_markdown(eid), winners[0])

    def test_external_change_rejects_old_hash(self):
        eid = library.save_document("原文", "外部")
        version = library.document_snapshot(eid)
        library.markdown_path(eid).write_text("外部编辑", encoding="utf-8")
        with self.assertRaises(library.RevisionConflict):
            library.save_markdown(eid, "旧结果", expected_hash=version["content_hash"])
        self.assertEqual(library.load_markdown(eid), "外部编辑")

    def test_parallel_append_keeps_every_section(self):
        eid = library.save_document("原文", "追加")
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: library.append_markdown_section(eid, str(n), f"结果{n}"), range(20)))
        result = library.load_markdown(eid)
        self.assertEqual(result.count("## "), 20)

    def test_bad_folder_index_never_becomes_empty(self):
        for body in ('{broken', '[]', '{"folders":[null]}'):
            with self.subTest(body=body):
                library.FOLDERS_FILE.write_text(body)
                with self.assertRaises(library.FolderIndexError):
                    library.create_folder("transcript", "新文件夹")
                self.assertEqual(library.FOLDERS_FILE.read_text(), body)

    def test_folder_delete_write_failure_leaves_retryable_folder(self):
        folder = library.create_folder("transcript", "文件夹")
        eid = library.save_document("保留", "文稿", folder_id=folder)
        with patch.object(library, "_write_raw", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                library.delete_folder(folder)
        self.assertEqual(library.list_folders("transcript")[0]["id"], folder)
        library.delete_folder(folder)
        self.assertEqual(library.load_markdown(eid), "保留")
        self.assertIsNone(library.document_snapshot(eid)["folder_id"])

    def test_move_rejects_other_kind_and_unknown_folder(self):
        eid = library.save_document("正文", "文稿")
        folder = library.create_folder("voice", "音频")
        for target in (folder, "unknown"):
            with self.assertRaises(ValueError):
                library.set_entry_folder(eid, target)
        self.assertIsNone(library.document_snapshot(eid)["folder_id"])

    def test_parallel_folder_creation_keeps_all_folders(self):
        with ThreadPoolExecutor(8) as pool:
            ids = list(pool.map(lambda n: library.create_folder("transcript", str(n)), range(20)))
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len(library.list_folders("transcript")), 20)

    def test_entry_id_cannot_escape_library(self):
        for eid in ("../outside", "a/b", "folders", ""):
            with self.assertRaises(ValueError):
                library.save_markdown(eid, "bad")

    def test_two_processes_with_same_original_have_only_one_success(self):
        import subprocess
        import sys
        eid = library.save_document("原文", "并发")
        version = library.document_snapshot(eid)
        script = """
import sys
from pathlib import Path
from unittest.mock import patch
with patch.object(Path, 'home', return_value=Path(sys.argv[2])):
 sys.path.insert(0, sys.argv[1])
 from src.core import library
 library.LIBRARY_DIR = Path(sys.argv[2])
 library.FOLDERS_FILE = library.LIBRARY_DIR / 'folders.json'
 sys.stdin.readline()
 try:
  library.save_markdown(sys.argv[3], sys.argv[5], expected_hash=sys.argv[4])
  print('saved')
 except library.RevisionConflict:
  print('conflict')
"""
        children = [subprocess.Popen([sys.executable, "-I", "-B", "-c", script,
            str(Path(__file__).resolve().parents[1]), str(library.LIBRARY_DIR), eid,
            version["content_hash"], text], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for text in ("GUI 修改", "CLI 修改")]
        for child in children:
            self.addCleanup(lambda p=child: p.kill() if p.poll() is None else None)
            child.stdin.write("go\n")
            child.stdin.flush()
        outcomes = []
        for child in children:
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error)
            outcomes.append(output.strip())
        self.assertCountEqual(outcomes, ["saved", "conflict"])
