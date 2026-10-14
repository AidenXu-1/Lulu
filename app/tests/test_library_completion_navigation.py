import unittest
from types import SimpleNamespace

from src.core.completion_feedback import CompletionTarget
from src.ui.main_window import MainWindow


class LibraryCompletionNavigationTests(unittest.TestCase):
    def test_saved_transcript_opens_exact_library_entry(self):
        calls = []
        shell = SimpleNamespace(
            open_material_entry=lambda entry_id, kind=None: calls.append((entry_id, kind)),
            _switch_page=lambda index: calls.append(("page", index)),
        )
        MainWindow._open_completion_target(
            shell,
            CompletionTarget(page_index=2, library_kind="transcript", entry_id="entry-7"),
        )
        self.assertEqual(calls, [("entry-7", "transcript")])

    def test_business_result_without_library_id_opens_its_existing_page(self):
        calls = []
        shell = SimpleNamespace(
            open_material_entry=lambda *_args, **_kwargs: calls.append("material"),
            _switch_page=lambda index: calls.append(index),
        )
        MainWindow._open_completion_target(shell, CompletionTarget(page_index=7))
        self.assertEqual(calls, [7])


if __name__ == "__main__":
    unittest.main()
