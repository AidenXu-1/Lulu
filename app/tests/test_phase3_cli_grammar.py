"""Phase 3 closed-grammar contract tests."""

from __future__ import annotations

import unittest

from src.agent_cli.grammar import parse_cli_argv


class Phase3CliGrammarTests(unittest.TestCase):
    def test_registered_command_is_discovered_from_the_bundled_closed_grammar(self) -> None:
        parsed = parse_cli_argv(["auth", "refresh", "--output", "json"])

        self.assertEqual(parsed.capability_id, "AUTH-03")
        self.assertEqual(parsed.command_id, "cap.auth-03")
        self.assertEqual(parsed.command_path, ("auth", "refresh"))
        self.assertEqual(parsed.arguments, {})
        self.assertEqual(parsed.output_format, "json")
        self.assertEqual(parsed.current_state, "unavailable")

    def test_unknown_command_returns_closed_error_without_reflecting_input(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(["warp-drive-private-value", "--output", "json"])

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "UNKNOWN_COMMAND")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(getattr(error, "details", None), {})
        self.assertNotIn("warp-drive-private-value", str(error))

    def test_registered_flag_and_closed_enum_are_parsed_from_catalog_template(self) -> None:
        parsed = parse_cli_argv(
            ["content", "save", "--kind", "rewrite", "--output", "json"]
        )

        self.assertEqual(parsed.capability_id, "LIB-15")
        self.assertEqual(parsed.command_path, ("content", "save"))
        self.assertEqual(parsed.arguments, {"kind": "rewrite"})

    def test_out_of_range_enum_is_invalid_without_reflecting_the_value(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                [
                    "content",
                    "save",
                    "--kind",
                    "private-invalid-kind",
                    "--output",
                    "json",
                ]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "kind", "reason_code": "VALUE_OUTSIDE_CLOSED_ENUM"},
        )
        self.assertNotIn("private-invalid-kind", str(error))

    def test_unknown_field_is_rejected_without_reflecting_name_or_value(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                [
                    "auth",
                    "refresh",
                    "--private-field",
                    "private-value",
                    "--output",
                    "json",
                ]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "UNKNOWN_FIELD")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(
            getattr(error, "details", None),
            {"reason_code": "OPTION_NOT_REGISTERED"},
        )
        self.assertNotIn("private-field", str(error))
        self.assertNotIn("private-value", str(error))

    def test_duplicate_registered_option_is_rejected_before_value_use(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                [
                    "content",
                    "save",
                    "--kind",
                    "rewrite",
                    "--kind",
                    "voice_result",
                    "--output",
                    "json",
                ]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "kind", "reason_code": "DUPLICATE_OPTION"},
        )

    def test_file_path_is_lexically_rejected_when_not_absolute(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                ["transcribe", "prepare", "private.wav", "--output", "json"]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "file", "reason_code": "PATH_MUST_BE_ABSOLUTE"},
        )
        self.assertNotIn("private.wav", str(error))

    def test_placeholder_value_over_size_limit_is_rejected_without_echo(self) -> None:
        oversized_value = "private-value-" + ("x" * 4096)

        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                [
                    "recording",
                    "start",
                    "--source",
                    oversized_value,
                    "--output",
                    "json",
                ]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(getattr(error, "exit_code", None), 2)
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "value", "reason_code": "VALUE_TOO_LARGE"},
        )
        self.assertNotIn("private-value", str(error))

    def test_catalog_jsonl_output_template_is_recognized_without_execution(self) -> None:
        parsed = parse_cli_argv(["jobs", "wait", "--output", "jsonl"])

        self.assertEqual(parsed.capability_id, "JOB-02")
        self.assertEqual(parsed.command_path, ("jobs", "wait"))
        self.assertEqual(parsed.arguments, {"output": "jsonl"})
        self.assertEqual(parsed.output_format, "jsonl")
        self.assertEqual(parsed.current_state, "unavailable")

    def test_url_placeholder_wins_over_the_lexically_invalid_file_candidate(self) -> None:
        parsed = parse_cli_argv(
            [
                "transcribe",
                "run",
                "https://example.invalid/private-media",
                "--output",
                "json",
            ]
        )

        self.assertEqual(parsed.capability_id, "TR-05")
        self.assertEqual(
            parsed.arguments,
            {"url": "https://example.invalid/private-media"},
        )
        self.assertEqual(parsed.command_path, ("transcribe", "run"))

    def test_absolute_path_with_parent_traversal_is_lexically_rejected(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                [
                    "transcribe",
                    "prepare",
                    "/private/tmp/../private-media.wav",
                    "--output",
                    "json",
                ]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "file", "reason_code": "PATH_TRAVERSAL_FORBIDDEN"},
        )
        self.assertNotIn("private-media.wav", str(error))

    def test_relative_file_cannot_fall_through_to_the_url_placeholder(self) -> None:
        with self.assertRaises(Exception) as raised:
            parse_cli_argv(
                ["transcribe", "run", "private.wav", "--output", "json"]
            )

        error = raised.exception
        self.assertEqual(getattr(error, "code", None), "INVALID_ARGUMENT")
        self.assertEqual(
            getattr(error, "details", None),
            {"field": "file", "reason_code": "PATH_MUST_BE_ABSOLUTE"},
        )
        self.assertNotIn("private.wav", str(error))


if __name__ == "__main__":
    unittest.main()
