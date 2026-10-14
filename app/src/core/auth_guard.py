"""Qt-free authorization guard for protected Core services."""

from __future__ import annotations

from .auth_contracts import (
    AuthorizationError,
    AuthorizationFailureV1,
    HumanSecretInputHandoffV1,
    SecretInputSource,
    SecretPurpose,
)


_AUTH_REQUIRED_MESSAGE = "需要有效的 Lulu 授权后才能执行此操作。"
_SECURE_STORE_MESSAGE = "系统安全存储不可用，请在可信 GUI 中检查授权状态。"
_FORBIDDEN_SECRET_SOURCES = frozenset(
    {"argv", "env", "stdin", "pipe", "pty", "tty", "config", "chat", "agent", "skill"}
)
_SECRET_PURPOSES = frozenset({"license_activation", "api_key"})


class HumanSecretInputPolicy:
    """Refuse machine-carried secrets and return only a human handoff."""

    __slots__ = ()

    def request_handoff(
        self,
        source_channel: SecretInputSource,
        purpose: SecretPurpose,
    ) -> HumanSecretInputHandoffV1:
        if source_channel not in _FORBIDDEN_SECRET_SOURCES:
            raise ValueError("未知的秘密输入来源")
        if purpose not in _SECRET_PURPOSES:
            raise ValueError("未知的秘密输入用途")
        return HumanSecretInputHandoffV1(
            code="HUMAN_HANDOFF_REQUIRED",
            destination="trusted_gui_or_os",
            source_channel=source_channel,
            purpose=purpose,
        )


class OpenProductAccessGuard:
    """Explicit free-product policy; installation and resource trust stay separate.

    No license service, cached entitlement, network or secret store participates.
    A retired application context can still be closed without changing licenses.
    """

    __slots__ = ("_revoked",)

    def __init__(self) -> None:
        self._revoked = False

    def revoke(self) -> None:
        self._revoked = True

    def assert_local_file_transcription_allowed(self, request: object) -> None:
        del request
        if self._revoked:
            raise AuthorizationError(
                AuthorizationFailureV1("AUTH_REQUIRED", "当前工作台已关闭，请重新打开 Lulu。")
            )


class UnifiedLicenseAuthGuard:
    """Ask one bound LicenseService for a fresh read-only projection per call."""

    __slots__ = ("_license_service", "_revoked")

    def __init__(self, license_service) -> None:
        if not callable(getattr(license_service, "cached_status_projection", None)):
            raise TypeError("license_service 必须提供 cached_status_projection()")
        self._license_service = license_service
        self._revoked = False

    @property
    def license_service(self):
        return self._license_service

    def revoke(self) -> None:
        self._revoked = True

    def assert_local_file_transcription_allowed(self, request: object) -> None:
        del request
        if self._revoked:
            raise AuthorizationError(
                AuthorizationFailureV1("AUTH_REQUIRED", _AUTH_REQUIRED_MESSAGE)
            )
        status = self._license_service.cached_status_projection()
        if status.allowed is not True:
            code = (
                "SECURE_STORE_UNAVAILABLE"
                if status.code == "secure_store_error"
                else "AUTH_REQUIRED"
            )
            message = (
                _SECURE_STORE_MESSAGE
                if code == "SECURE_STORE_UNAVAILABLE"
                else _AUTH_REQUIRED_MESSAGE
            )
            raise AuthorizationError(AuthorizationFailureV1(code, message))
