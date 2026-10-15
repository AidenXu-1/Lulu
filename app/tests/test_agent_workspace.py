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


if __name__ == "__main__":
    unittest.main()
