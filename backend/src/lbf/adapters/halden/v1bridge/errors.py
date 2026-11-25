"""Integration error taxonomy."""

from __future__ import annotations


class BridgeError(Exception):
    """Base error of the integration. ``code`` is stable and used by tools and the UI."""

    default_code = "BRIDGE_ERROR"
    default_retryable = False

    def __init__(self, code: str | None = None, message: str = "", retryable: bool | None = None):
        self.code = code or self.default_code
        self.message = message or self.code
        self.retryable = self.default_retryable if retryable is None else retryable
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class LegacyAuthError(BridgeError):
    default_code = "AUTH_FAILED"

    def __init__(self, message: str = "HALDEN ERP rejected the integration credentials."):
        super().__init__(None, message)


class LegacyUnavailable(BridgeError):
    default_code = "LEGACY_UNAVAILABLE"
    default_retryable = True

    def __init__(self, message: str = "HALDEN ERP is not responding."):
        super().__init__(None, message)


class LegacyRateLimited(BridgeError):
    default_code = "RATE_LIMITED"
    default_retryable = True

    def __init__(self, message: str = "HALDEN ERP is throttling requests; try again shortly."):
        super().__init__(None, message)


class LegacyMalformedResponse(BridgeError):
    default_code = "MALFORMED_RESPONSE"

    def __init__(self, message: str = "HALDEN ERP returned data the bridge cannot interpret."):
        super().__init__(None, message)


class NotFoundError(BridgeError):
    default_code = "NOT_FOUND"

    def __init__(self, message: str = "Not found."):
        super().__init__(None, message)


class DraftStateError(BridgeError):
    default_code = "DRAFT_STATE"

    def __init__(self, message: str = "The draft does not permit this action."):
        super().__init__(None, message)
