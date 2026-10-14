"""Phase 8 single product storage-layout projection tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest


class Phase8SingleStorageLayoutTests(unittest.TestCase):
    def test_gui_and_agent_cli_resolve_the_same_explicit_zero_write_layout(self) -> None:
        try:
            from src.core.storage_layout import SharedProductStorageLayout
        except ImportError as exc:
            self.fail(f"Phase 8 shared storage layout seam is missing: {exc}")

        with tempfile.TemporaryDirectory() as directory:
            application_support = Path(directory).resolve()
            before = tuple(application_support.iterdir())
            layout = SharedProductStorageLayout.from_application_support_root(
                application_support
            )
            gui = layout.project("gui")
            console = layout.project("agent_cli")
            after = tuple(application_support.iterdir())

        expected_paths = {
            "product_root": str(application_support / "Lulu"),
            "config": str(application_support / "Lulu" / "config"),
            "jobs": str(application_support / "Lulu" / "jobs"),
            "models": str(application_support / "Lulu" / "models"),
            "library": str(application_support / "Lulu" / "library"),
            "voices": str(application_support / "Lulu" / "voices"),
            "runtime": str(application_support / "Lulu" / "runtime"),
        }
        self.assertEqual(before, ())
        self.assertEqual(after, ())
        self.assertEqual(gui["schema_id"], "lulu.shared-storage/v1")
        self.assertEqual(console["schema_id"], "lulu.shared-storage/v1")
        self.assertEqual(gui["paths"], expected_paths)
        self.assertEqual(console["paths"], expected_paths)
        self.assertEqual(gui["adapter_id"], "gui")
        self.assertEqual(console["adapter_id"], "agent_cli")
        self.assertFalse(any("agent" in path.lower() for path in expected_paths.values()))


if __name__ == "__main__":
    unittest.main()
