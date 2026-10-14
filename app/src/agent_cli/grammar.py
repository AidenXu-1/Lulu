"""Closed Phase 3 grammar derived only from the bundled capability snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import urlsplit

from .catalog import load_capability_catalog, active_capabilities


_SECRET_OPTIONS = frozenset(
    {
        "--api-key",
        "--apikey",
        "--license-code",
        "--license-key",
        "--secret",
        "--token",
        "--access-token",
        "--auth-token",
        "--bearer-token",
        "--credential",
        "--password",
    }
)


class CliGrammarError(Exception):
    """Closed, non-reflective command-line validation failure."""

    def __init__(
        self,
        code: str,
        *,
        exit_code: int = 2,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ParsedCliCommand:
    capability_id: str
    command_id: str
    command_path: tuple[str, ...]
    arguments: dict[str, str]
    output_format: str
    current_state: str


def _match_registered_template(
    template_tokens: list[str],
    actual_tokens: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, str], int] | None:
    if len(template_tokens) != len(actual_tokens):
        return None
    command_path: list[str] = []
    arguments: dict[str, str] = {}
    specificity = 0
    for index, (expected, actual) in enumerate(zip(template_tokens, actual_tokens)):
        if expected.startswith("<") and expected.endswith(">"):
            if actual.startswith("--"):
                return None
            arguments[expected[1:-1]] = actual
            continue
        if expected.startswith("{") and expected.endswith("}"):
            if actual not in expected[1:-1].split("|"):
                return None
            previous = template_tokens[index - 1] if index else ""
            if previous.startswith("--"):
                arguments[previous[2:].replace("-", "_")] = actual
            else:
                command_path.append(actual)
            specificity += 1
            continue
        if "|" in expected:
            if actual not in expected.split("|"):
                return None
            previous = template_tokens[index - 1] if index else ""
            if previous.startswith("--"):
                arguments[previous[2:].replace("-", "_")] = actual
            else:
                command_path.append(actual)
            specificity += 1
            continue
        if expected != actual:
            return None
        if expected.startswith("--"):
            next_token = template_tokens[index + 1] if index + 1 < len(template_tokens) else ""
            if not next_token or next_token.startswith("--"):
                arguments[expected[2:].replace("-", "_")] = "true"
        else:
            previous = template_tokens[index - 1] if index else ""
            if previous.startswith("--"):
                arguments[previous[2:].replace("-", "_")] = actual
            else:
                command_path.append(actual)
        specificity += 2
    return tuple(command_path), arguments, specificity


def _closed_enum_violation(
    template_tokens: list[str],
    actual_tokens: tuple[str, ...],
) -> str | None:
    if len(template_tokens) != len(actual_tokens):
        return None
    invalid_field: str | None = None
    for index, (expected, actual) in enumerate(zip(template_tokens, actual_tokens)):
        if expected.startswith("<") and expected.endswith(">"):
            if actual.startswith("--"):
                return None
            continue
        if expected.startswith("{") and expected.endswith("}"):
            if actual in expected[1:-1].split("|"):
                continue
            previous = template_tokens[index - 1] if index else ""
            if not previous.startswith("--") or invalid_field is not None:
                return None
            invalid_field = previous[2:].replace("-", "_")
            continue
        if "|" in expected:
            if actual not in expected.split("|"):
                return None
            continue
        if expected != actual:
            return None
    return invalid_field


def _literal_command_prefix(template_tokens: list[str]) -> tuple[str, ...]:
    prefix: list[str] = []
    for token in template_tokens:
        if (
            token.startswith("--")
            or token.startswith("<")
            or token.startswith("{")
            or "|" in token
        ):
            break
        prefix.append(token)
    return tuple(prefix)


def _validate_argument_constraints(arguments: dict[str, str]) -> None:
    for field, value in arguments.items():
        if len(value.encode("utf-8")) > 4096:
            raise CliGrammarError(
                "INVALID_ARGUMENT",
                details={"field": field, "reason_code": "VALUE_TOO_LARGE"},
            )
    for field in ("file", "directory", "json_ref"):
        value = arguments.get(field)
        if value is None:
            continue
        path = PurePath(value)
        if ".." in path.parts:
            raise CliGrammarError(
                "INVALID_ARGUMENT",
                details={"field": field, "reason_code": "PATH_TRAVERSAL_FORBIDDEN"},
            )
        if not path.is_absolute():
            raise CliGrammarError(
                "INVALID_ARGUMENT",
                details={"field": field, "reason_code": "PATH_MUST_BE_ABSOLUTE"},
            )
    url = arguments.get("url")
    if url is not None:
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise CliGrammarError(
                "INVALID_ARGUMENT",
                details={"field": "url", "reason_code": "URL_FORMAT_INVALID"},
            )


def parse_cli_argv(argv: list[str]) -> ParsedCliCommand:
    """Resolve a registered command without consulting project files."""
    if any(
        token.split("=", 1)[0].lower().replace("_", "-") in _SECRET_OPTIONS
        for token in argv
    ):
        raise CliGrammarError(
            "INVALID_ARGUMENT",
            details={"reason_code": "SECRET_INPUT_FORBIDDEN"},
        )
    if len(argv) >= 3 and argv[-2:] == ["--output", "json"]:
        command_path = tuple(argv[:-2])
        output_format: str | None = "json"
    else:
        command_path = tuple(argv)
        output_format = None
    option_tokens = [token for token in command_path if token.startswith("--")]
    duplicate_options = sorted(
        {option for option in option_tokens if option_tokens.count(option) > 1}
    )
    if duplicate_options:
        field = duplicate_options[0][2:].replace("-", "_")
        raise CliGrammarError(
            "INVALID_ARGUMENT",
            details={"field": field, "reason_code": "DUPLICATE_OPTION"},
        )
    catalog = load_capability_catalog()["catalog"]
    matches: list[tuple[int, dict[str, object], tuple[str, ...], dict[str, str]]] = []
    for capability in active_capabilities(catalog):
        template_tokens = capability["argv_template"].split()
        if not template_tokens or template_tokens[0] != "lulu":
            continue
        matched = _match_registered_template(template_tokens[1:], command_path)
        if matched is not None:
            registered_path, arguments, specificity = matched
            matches.append((specificity, capability, registered_path, arguments))
    if matches:
        validation_errors: list[CliGrammarError] = []
        for _, capability, registered_path, arguments in sorted(
            matches,
            key=lambda item: item[0],
            reverse=True,
        ):
            candidate_output_format = output_format
            if candidate_output_format is None:
                if (
                    capability["capability_id"] != "JOB-02"
                    or arguments.get("output") != "jsonl"
                ):
                    validation_errors.append(
                        CliGrammarError(
                            "INVALID_ARGUMENT",
                            details={"reason_code": "JSON_OUTPUT_REQUIRED"},
                        )
                    )
                    continue
                candidate_output_format = "jsonl"
            try:
                _validate_argument_constraints(arguments)
            except CliGrammarError as exc:
                validation_errors.append(exc)
                continue
            return ParsedCliCommand(
                capability_id=capability["capability_id"],
                command_id=capability["command_id"],
                command_path=registered_path,
                arguments=arguments,
                output_format=candidate_output_format,
                current_state=capability["current_cli_state"],
            )
        raise validation_errors[0]
    for capability in active_capabilities(catalog):
        template_tokens = capability["argv_template"].split()
        if not template_tokens or template_tokens[0] != "lulu":
            continue
        invalid_field = _closed_enum_violation(template_tokens[1:], command_path)
        if invalid_field is not None:
            raise CliGrammarError(
                "INVALID_ARGUMENT",
                details={
                    "field": invalid_field,
                    "reason_code": "VALUE_OUTSIDE_CLOSED_ENUM",
                },
            )
    for capability in active_capabilities(catalog):
        template_tokens = capability["argv_template"].split()
        if not template_tokens or template_tokens[0] != "lulu":
            continue
        prefix = _literal_command_prefix(template_tokens[1:])
        if (
            prefix
            and command_path[: len(prefix)] == prefix
            and any(token.startswith("--") for token in command_path[len(prefix) :])
        ):
            raise CliGrammarError(
                "UNKNOWN_FIELD",
                details={"reason_code": "OPTION_NOT_REGISTERED"},
            )
    raise CliGrammarError("UNKNOWN_COMMAND")
