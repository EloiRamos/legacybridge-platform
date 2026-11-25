"""RFC 9457 problem details. Bodies carry only safe text and the ``request_id`` (never internals)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from lbf.domain.errors import HTTP_STATUS, DomainError, ErrorCode

TITLES = {
    ErrorCode.UNAUTHENTICATED: "Authentication required", ErrorCode.REAUTH_REQUIRED: "Re-authentication required",
    ErrorCode.CSRF_FAILED: "CSRF check failed", ErrorCode.FORBIDDEN: "Forbidden",
    ErrorCode.SOD_VIOLATION: "Segregation of duties", ErrorCode.NOT_FOUND: "Not found",
    ErrorCode.VALIDATION: "Validation failed", ErrorCode.CONFLICT_VERSION: "Version conflict",
    ErrorCode.CONFLICT_STATE: "State conflict", ErrorCode.CONFLICT_OVERLAP: "Overlapping operation",
    ErrorCode.APPROVAL_STALE_VALIDATION: "Validation too old", ErrorCode.RATE_LIMITED: "Rate limited",
    ErrorCode.BACKEND_UNAVAILABLE: "Backend unavailable", ErrorCode.BACKEND_AUTH: "Backend refused credentials",
    ErrorCode.BACKEND_THROTTLED: "Backend throttled", ErrorCode.BACKEND_MALFORMED: "Backend data unreadable",
    ErrorCode.CAPABILITY_UNSUPPORTED: "Not supported by this backend", ErrorCode.INTERNAL: "Internal error",
}


def problem(request: Request, code: ErrorCode, detail: str = "", *, status: int | None = None,
            extra: dict[str, Any] | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body: dict[str, Any] = {
        "type": f"https://legacybridge.invalid/problems/{code.value.lower()}", "title": TITLES.get(code, code.value),
        "status": status or HTTP_STATUS[code], "code": code.value, "detail": detail, "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=body["status"], media_type="application/problem+json")


def from_domain(request: Request, exc: DomainError) -> JSONResponse:
    safe = {k: v for k, v in exc.detail.items() if k in ("checks", "verdict", "detail")}
    return problem(request, exc.code, exc.message, extra=safe or None)
