"""Active file-dialog filters use Qt's ``;;`` separators."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "ui"
EXPECTED_CALLS = {
    "library_page.py": 1,
    "main_window.py": 2,
    "voice_page.py": 3,
}


class QtFileFilterTests(unittest.TestCase):
    def test_target_dialogs_have_only_qt_double_semicolon_separators(self):
        inspected = 0
        for name, expected_count in EXPECTED_CALLS.items():
            path = UI_ROOT / name
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            calls = [
                node
                for node in ast.walk(tree)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"getOpenFileName", "getOpenFileNames", "getSaveFileName"}
                    and any(
                        isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and "所有文件" in item.value
                        for item in ast.walk(node)
                    )
                )
            ]
            self.assertEqual(
                len(calls),
                expected_count,
                f"{name} target file-dialog count drifted",
            )
            for call in calls:
                literals = [
                    item.value
                    for item in ast.walk(call)
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                ]
                rendered = "".join(literals)
                self.assertNotIn("；", rendered, f"{name}:{call.lineno}")
                self.assertIn(";;所有文件", rendered, f"{name}:{call.lineno}")
                inspected += 1
        self.assertEqual(inspected, sum(EXPECTED_CALLS.values()))


if __name__ == "__main__":
    unittest.main()
