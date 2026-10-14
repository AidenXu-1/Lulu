import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import library
from src.core.agent_workspace import documents
from src.core.agent_workspace.service import WorkspaceService, validate_request
from src.core.agent_workspace.store import WorkspaceStore
from src.core.models import Transcript


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        p = patch('src.core.agent_workspace.skills.skill_root', return_value=self.root/'skills')
        p.start(); self.addCleanup(p.stop)
        for name, value in (("LIBRARY_DIR", self.root / "library"),
                            ("FOLDERS_FILE", self.root / "library/folders.json")):
            p = patch.object(library, name, value)
            p.start(); self.addCleanup(p.stop)
        self.service = WorkspaceService()
        self.eid = library.save_document("第一段🙂。\n\n第一段🙂。", "文稿")

    def request(self, markdown, request_id="save1"):
        doc = documents.snapshot(self.eid)
        return dict(document_id=self.eid, markdown=markdown, title=doc["title"],
                    expected_revision=doc["revision"], expected_hash=doc["content_hash"], request_id=request_id)

    def test_visible_count_cache_is_exact_bounded_and_skips_oversized_input(self):
        from src.core import document_metrics as metrics
        metrics._cached_visible_characters.cache_clear()
        self.addCleanup(metrics._cached_visible_characters.cache_clear)
        with patch.object(metrics, '_count', wraps=metrics._count) as count:
            self.assertEqual(metrics.visible_characters('**同一正文**'),4)
            self.assertEqual(metrics.visible_characters('**同一正文**'),4)
            self.assertEqual(count.call_count,1)
            self.assertEqual(metrics.visible_characters('**同一正文**增加'),6)
            for i in range(12): metrics.visible_characters('不同稿'+str(i))
            self.assertEqual(metrics._cached_visible_characters.cache_info().currsize,8)
            large='字'*262145
            metrics.visible_characters(large)
            before=count.call_count
            metrics.visible_characters(large)
            self.assertEqual(count.call_count,before+1)

    def test_visible_count_matches_list_open_save_and_external_changes(self):
        from src.core.document_metrics import visible_characters
        text = '## 标题\n\n> **你好** [Lulu](https://example.test) 🙂\n\n`a b`'
        expected = len('标题你好Lulu🙂ab')
        self.assertEqual(visible_characters(text), expected)
        self.assertEqual(visible_characters('---\ntitle: 元数据\n---\n' + text), expected)
        saved = self.service.save(**self.request(text))
        self.assertEqual(saved['display_char_count'], expected)
        self.assertEqual(self.service.open(self.eid)['document']['display_char_count'], expected)
        self.assertEqual(self.service.list()['entries'][0]['display_char_count'], expected)
        # Display projection must not change authoritative revision/receipt data.
        self.assertEqual(self.service.save(**self.request(text, 'same2'))['display_char_count'], expected)
        library.markdown_path(self.eid).write_text(text + '\n\n新内容', encoding='utf-8')
        self.assertEqual(self.service.list()['entries'][0]['display_char_count'], expected + 3)
        self.assertEqual(self.service.snapshot(self.eid)['display_char_count'], expected + 3)

    def test_crash_at_each_write_recovers_or_keeps_old_document(self):
        original = library._write_atomic_text
        for fail_at in range(1, 8):
            with self.subTest(fail_at=fail_at):
                request = self.request("修订" + str(fail_at), "crash" + str(fail_at))
                count = 0
                def fail(path, value):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise OSError("interrupted write")
                    return original(path, value)
                with patch.object(library, "_write_atomic_text", side_effect=fail):
                    try:
                        documents.save(**request)
                    except OSError:
                        pass
                # Reader can recover before writer retries, including before receipt creation.
                documents.snapshot(self.eid)
                result = documents.save(**request)
                self.assertEqual(documents.snapshot(self.eid)["text"], request["markdown"])
                self.assertEqual(documents.save(**request), result)

    def test_external_changes_cannot_be_overwritten(self):
        request = self.request("我的草稿")
        library.markdown_path(self.eid).write_text("外部修改", encoding="utf-8")
        with self.assertRaises(library.RevisionConflict):
            documents.save(**request)
        self.assertEqual(documents.snapshot(self.eid)["text"], "外部修改")

    def test_partial_apply_and_late_retry_preserve_all_decisions(self):
        doc = documents.snapshot(self.eid)
        chat = self.service.new_conversation(self.eid, "chat1")
        before = "第一段🙂。"
        size = len(before.encode())
        operations = [dict(id="op1", action="replace", start=0, end=size, before_hash=documents.digest(before), markdown="短。"),
                      dict(id="op2", action="replace", start=size+2, end=size*2+2, before_hash=documents.digest(before), markdown="较长的第二段。")]
        self.service.proposal_plan(chat["id"], "p1", doc["revision"], doc["content_hash"], operations)
        self.service.proposal_apply("p1", ["op1"], "apply1")
        self.service.proposal_apply("p1", ["op2"], "apply2")
        replay = self.service.proposal_apply("p1", ["op1"], "apply1")
        self.assertEqual(replay["proposal"]["state"], "applied")
        self.assertEqual(documents.snapshot(self.eid)["text"], "短。\n\n较长的第二段。")

    def test_utf8_boundaries_overlap_and_wrong_content_are_rejected(self):
        for start, end, hash_value in [(1, 4, documents.digest("")), (0, 3, "wrong")]:
            with self.assertRaises((ValueError, library.RevisionConflict)):
                documents.apply_operations("原文", [dict(action="replace", start=start, end=end, before_hash=hash_value, markdown="新")])
        op = dict(action="replace", start=0, end=6, before_hash=documents.digest("原文"), markdown="新")
        with self.assertRaises(ValueError):
            documents.apply_operations("原文", [op, op])

    def test_conversations_drafts_persist_and_do_not_cross_documents(self):
        chat = self.service.new_conversation(self.eid, "chat1")
        draft = dict(text="片段一：\n片段二：", references=[{"id": "ref2", "label_number": 2}], next_reference=3)
        self.service.save_draft(chat["id"], draft, 0)
        with self.assertRaises(library.RevisionConflict):
            self.service.save_draft(chat["id"], {"text": "stale"}, 0)
        reopened = WorkspaceStore()
        self.assertEqual(reopened.get(chat["id"])["draft"], draft)
        other = library.save_document("另一篇", "另一篇")
        with self.assertRaises(library.RevisionConflict):
            reopened.create(other, chat["id"])
        self.assertEqual(reopened.list(other), [])
        reopened.append(chat["id"], "user", "修改", message_id="m1")
        reopened.append(chat["id"], "user", "修改", message_id="m1")
        self.assertEqual(len(reopened.messages(chat["id"])), 1)

    def test_unified_query_preserves_ids_and_folders_and_searches_author(self):
        rewrite = library.save_rewrite("改写正文", "旧输出", source_title="源文稿")
        fid = library.create_folder("transcript", "文件夹")
        library.set_entry_folder(rewrite, fid)
        author = library.save(Transcript("正文", meta={"uploader": "作者甲"}), "作者作品")
        result = self.service.list()
        self.assertEqual({r["id"] for r in result["entries"]}, {self.eid, rewrite, author})
        self.assertEqual(result["folders"][0]["count"], 1)
        self.assertEqual(self.service.list(search="作者甲")["entries"][0]["id"], author)
        self.assertEqual(library._load_raw(rewrite)["kind"], "rewrite")

    def test_create_retry_and_closed_request_schema(self):
        first = self.service.create_document("create1")
        self.assertEqual(first, self.service.create_document("create1"))
        for operation, args in [("__dict__", {}), ("save", {"path": "/tmp/arbitrary"}), ("list", {"bad": True})]:
            with self.assertRaises((ValueError, TypeError)):
                validate_request(operation, args)


if __name__ == "__main__":
    unittest.main()
