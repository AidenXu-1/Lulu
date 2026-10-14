from __future__ import annotations

from io import StringIO
import json
import unittest


class CliUrlPlanTests(unittest.TestCase):
    def test_url_prepare_returns_complete_default_plan_without_effects(self) -> None:
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
        )

        boundary_calls: list[str] = []
        service = LinkTranscriptionApplicationService(
            installation_id="installation-test-001",
            pipeline_runner=lambda **_kwargs: boundary_calls.append("pipeline"),
        )

        plan = service.prepare("HTTPS://Example.COM:443/watch?v=42#fragment")

        self.assertEqual(
            plan["typed_request"],
            {
                "source": "https://example.com/watch?v=42",
                "engine": "local_sensevoice",
                "format": "txt",
                "prefer_subtitles": False,
                "subtitle_fallback": "local_transcription",
                "save_media": False,
                "media_mode": "audio",
                "media_directory": None,
                "save_cover": False,
                "output_destination": "shared_lulu_transcript_default",
            },
        )
        self.assertEqual(plan["effects"]["network"], True)
        self.assertEqual(plan["targets"]["transcript"], "shared_lulu_transcript_default")
        self.assertEqual(plan["missing_questions"], ["confirm_current_settings"])
        self.assertRegex(plan["plan_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(boundary_calls, [])

        from src.agent_cli.main import main

        stdout = StringIO()
        stderr = StringIO()
        exit_code = main(
            [
                "transcribe",
                "prepare",
                "HTTPS://Example.COM:443/watch?v=42#fragment",
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=stderr,
            link_transcription_service=service,
        )
        self.assertEqual(exit_code, 0, stderr.getvalue())
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["command"], "transcribe.prepare")
        self.assertEqual(envelope["result"], plan)
        self.assertEqual(boundary_calls, [])

        stdout = StringIO()
        exit_code = main(
            [
                "transcribe",
                "prepare",
                "https://example.com/watch?v=42",
                "--engine",
                "local_sensevoice",
                "--format",
                "srt",
                "--prefer-subtitles",
                "--subtitle-fallback",
                "fail",
                "--save-media",
                "--media-mode",
                "video",
                "--media-directory",
                "/tmp/lulu-media",
                "--save-cover",
                "--output-destination",
                "shared_lulu_transcript_default",
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=StringIO(),
            link_transcription_service=service,
        )
        self.assertEqual(exit_code, 0)
        configured = json.loads(stdout.getvalue())["result"]["typed_request"]
        self.assertEqual(configured["format"], "srt")
        self.assertTrue(configured["prefer_subtitles"])
        self.assertEqual(configured["subtitle_fallback"], "fail")
        self.assertTrue(configured["save_media"])
        self.assertEqual(configured["media_mode"], "video")
        self.assertTrue(configured["save_cover"])


if __name__ == "__main__":
    unittest.main()
