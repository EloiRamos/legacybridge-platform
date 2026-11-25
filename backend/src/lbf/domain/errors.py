"""Error taxonomy. The API maps these to RFC 9457 problem details."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"
    CSRF_FAILED = "CSRF_FAILED"
    FORBIDDEN = "FORBIDDEN"
    SOD_VIOLATION = "SOD_VIOLATION"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION = "VALIDATION"
    CONFLICT_VERSION = "CONFLICT_VERSION"
    CONFLICT_STATE = "CONFLICT_STATE"
    CONFLICT_OVERLAP = "CONFLICT_OVERLAP"
    APPROVAL_STALE_VALIDATION = "APPROVAL_STALE_VALIDATION"
    RATE_LIMITED = "RATE_LIMITED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_AUTH = "BACKEND_AUTH"
    BACKEND_THROTTLED = "BACKEND_THROTTLED"
    BACKEND_MALFORMED = "BACKEND_MALFORMED"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    ASSISTANT_BUDGET_EXHAUSTED = "ASSISTANT_BUDGET_EXHAUSTED"
    ASSISTANT_UNAVAILABLE = "ASSISTANT_UNAVAILABLE"
    INTERNAL = "INTERNAL"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.REAUTH_REQUIRED: 401,
    ErrorCode.CSRF_FAILED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SOD_VIOLATION: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VALIDATION: 422,
    ErrorCode.CONFLICT_VERSION: 409,
    ErrorCode.CONFLICT_STATE: 409,
    ErrorCode.CONFLICT_OVERLAP: 409,
    ErrorCode.APPROVAL_STALE_VALIDATION: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DATABASE_UNAVAILABLE: 503,
    ErrorCode.BACKEND_UNAVAILABLE: 503,
    ErrorCode.BACKEND_AUTH: 502,
    ErrorCode.BACKEND_THROTTLED: 503,
    ErrorCode.BACKEND_MALFORMED: 502,
    ErrorCode.CAPABILITY_UNSUPPORTED: 409,
    ErrorCode.ASSISTANT_BUDGET_EXHAUSTED: 200,
    ErrorCode.ASSISTANT_UNAVAILABLE: 200,
    ErrorCode.INTERNAL: 500,
}

_INTEGRATION_CODES = frozenset(
    {
        ErrorCode.BACKEND_UNAVAILABLE,
        ErrorCode.BACKEND_AUTH,
        ErrorCode.BACKEND_THROTTLED,
        ErrorCode.BACKEND_MALFORMED,
        ErrorCode.CAPABILITY_UNSUPPORTED,
    }
)


class DomainError(Exception):
    """A business-rule failure with a stable code and a safe message."""

    def __init__(self, code: ErrorCode, message: str = "", *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
        self.detail = detail or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]


class IntegrationError(DomainError):
    """A legacy-backend failure. ``retryable_read`` says whether a *read* may be retried; writes never are."""

    def __init__(self, code: ErrorCode, message: str = "", *, retryable_read: bool = False) -> None:
        if code not in _INTEGRATION_CODES:
            raise ValueError(f"{code} is not an integration error code")
        super().__init__(code, message)
        self.retryable_read = retryable_read


def capability_unsupported(method: str, backend_id: str) -> IntegrationError:
    return IntegrationError(
        ErrorCode.CAPABILITY_UNSUPPORTED, f"{method} is not supported by backend {backend_id}", retryable_read=False
    )
