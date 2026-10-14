import unittest
from pathlib import Path

from src.ui.voice_page import _finder_reveal_failed_message


class VoicePagePrivacyTests(unittest.TestCase):
    def test_finder_failure_message_does_not_include_local_path(self):
        sensitive_path = Path("/Users/zhaoji/Library/Application Support/Lulu/voices/private.wav")
        message = _finder_reveal_failed_message(OSError(f"open failed: {sensitive_path}"), sensitive_path)

        self.assertIn("无法在 Finder 中显示该文件", message)
        self.assertNotIn("/Users/", message)
        self.assertNotIn("zhaoji", message)
        self.assertNotIn("Library/Application Support", message)
        self.assertNotIn("private.wav", message)


if __name__ == "__main__":
    unittest.main()
