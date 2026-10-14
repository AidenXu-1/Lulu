"""Strict RFC 8785-style canonical JSON and domain-separated digests."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math


class CanonicalizationError(ValueError):
    """A value cannot be represented by the closed canonical JSON contract."""


def _canonical_number(value: int | float) -> str:
    if isinstance(value, bool):
        raise CanonicalizationError("boolean is not a JSON number")
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise CanonicalizationError("integer exceeds the I-JSON exact range")
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError("NaN and Infinity are forbidden")
    if value == 0:
        return "0"
    decimal = Decimal(repr(value))
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        rendered = format(decimal, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered
    sign, digits_tuple, exponent = decimal.as_tuple()
    digits = "".join(str(digit) for digit in digits_tuple)
    adjusted = len(digits) + exponent - 1
    fraction = digits[1:].rstrip("0")
    mantissa = digits[0] + (f".{fraction}" if fraction else "")
    exponent_text = f"+{adjusted}" if adjusted >= 0 else str(adjusted)
    return ("-" if sign else "") + mantissa + "e" + exponent_text


def _canonical_text(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("invalid Unicode is forbidden") from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_text(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise CanonicalizationError("object keys must be strings")
        try:
            keys = sorted(value, key=lambda item: item.encode("utf-16-be"))
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("invalid Unicode is forbidden") from exc
        return "{" + ",".join(
            f"{_canonical_text(key)}:{_canonical_text(value[key])}" for key in keys
        ) + "}"
    raise CanonicalizationError(f"unsupported JSON type: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Encode a closed JSON value deterministically as UTF-8."""
    return _canonical_text(value).encode("utf-8")


def parse_canonical_json(raw: bytes) -> object:
    """Parse canonical UTF-8 JSON while retaining duplicate-key detection."""
    if type(raw) is not bytes:
        raise CanonicalizationError("canonical JSON input must be bytes")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalizationError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise CanonicalizationError(f"forbidden JSON constant: {value}")

    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except CanonicalizationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalizationError("invalid canonical JSON") from exc
    if canonical_json_bytes(value) != raw:
        raise CanonicalizationError("JSON input is not canonical")
    return value


def domain_separated_sha256(domain: str, value: object) -> str:
    """Hash a canonical value with an unambiguous fixed UTF-8 domain prefix."""
    if not domain or "\x00" in domain:
        raise CanonicalizationError("invalid digest domain")
    preimage = domain.encode("ascii") + b"\x00" + canonical_json_bytes(value)
    return hashlib.sha256(preimage).hexdigest()
