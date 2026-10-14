"""Qt-free authorization and human-handoff contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


AuthorizationFailureCode: TypeAlias = Literal[
    "AUTH_REQUIRED",
    "SECURE_STORE_UNAVAILABLE",
]
SecretInputSource: TypeAlias = Literal[
    "argv",
    "env",
    "stdin",
    "pipe",
    "pty",
    "tty",
    "config",
    "chat",
    "agent",
    "skill",
]
SecretPurpose: TypeAlias = Literal["license_activation", "api_key"]


@dataclass(frozen=True, slots=True)
class AuthorizationFailureV1:
    code: AuthorizationFailureCode
    user_message: str


@dataclass(frozen=True, slots=True)
class HumanSecretInputHandoffV1:
    code: Literal["HUMAN_HANDOFF_REQUIRED"]
    destination: Literal["trusted_gui_or_os"]
    source_channel: SecretInputSource
    purpose: SecretPurpose
    accepted: Literal[False] = False


class AuthorizationError(Exception):
    """Carry one stable, sanitized authorization failure DTO."""

    def __init__(self, failure: AuthorizationFailureV1):
        if type(failure) is not AuthorizationFailureV1:
            raise TypeError("failure 必须是 AuthorizationFailureV1")
        self.failure = failure
        super().__init__(failure.user_message)
