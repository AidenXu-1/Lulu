"""Public integration seams for the verified Agent CLI runtime."""

from .contracts import CliRuntimeTrustError, CodeSignatureEvidenceV1
from .factory import build_production_runtime
from .installation_ticket import AuthorityError
from .keychain import (
    provision_local_trusted_test_credentials,
)

__all__ = [
    "AuthorityError",
    "CliRuntimeTrustError",
    "CodeSignatureEvidenceV1",
    "build_production_runtime",
    "provision_local_trusted_test_credentials",
]
