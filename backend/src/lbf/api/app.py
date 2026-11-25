"""FastAPI application factory: routers, RFC 9457 problems, security headers, request ids, static SPA.

The demo router (``lbf.demo``) is mounted **only** when ``LBF_PROFILE`` is ``demo`` or ``test`` (SEC-15); this is
the only production module allowed to import ``lbf.demo`` (boundary test B-7b).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from lbf.api import openapi_doc
from lbf.api.problems import from_domain, problem
from lbf.api.routers import admin, assistant, audit, auth, customers, health, inventory, operations, queues
from lbf.api.services import AppServices
from lbf.domain.errors import HTTP_STATUS, DomainError, ErrorCode

log = logging.getLogger("lbf.api")
API_VERSION = "v1"
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
# The connection may be lost while COMMIT is in flight, or after an earlier transaction of the same request: the text
# therefore never states that the request was (or was not) applied.
DATABASE_UNAVAILABLE_DETAIL = ("The database is unavailable. The request may or may not have been applied; check the "
                               "current state before repeating it.")
# SQLSTATEs that mean "no usable server connection": connection exceptions (class 08), shutdown/crash/startup, no slots.
_UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})
_PROBLEM_503: dict[int | str, dict[str, Any]] = {503: {
    "description": "A required database or backend is unavailable (RFC 9457 problem)",
    "content": {"application/problem+json": {"schema": {
        "title": "UnavailableProblem", "type": "object",
        "required": ["type", "title", "status", "code", "detail", "request_id"],
        "properties": {
            "type": {"type": "string"}, "title": {"type": "string"},
            "status": {"type": "integer", "enum": [503]},
            "code": {"type": "string", "enum": sorted(c.value for c, s in HTTP_STATUS.items() if s == 503)},
            "detail": {"type": "string"}, "request_id": {"type": "string"}}}}}}}


def database_unavailable(exc: Exception) -> bool:
    """True only for confirmed connection or pool unavailability; failed SQL on a healthy connection is not."""
    if isinstance(exc, PoolTimeoutError):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:  # an established connection was lost
        return True
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate.startswith("08") or sqlstate in _UNAVAILABLE_SQLSTATES
    # connecting failed: the driver reports no SQLSTATE and no statement was involved
    return isinstance(exc, OperationalError) and exc.statement is None


def create_app(svc: AppServices, *, start_engines: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_engines and svc.runner is not None:
            svc.runner.start()
        yield
        if start_engines and svc.runner is not None:
            svc.runner.stop()

    app = FastAPI(title="LEGACYBRIDGE Platform API", version="2.0.0",
                  description="Fictional engagement. Order operations over two SIMULATED legacy ERPs "
                              "(HALDEN ERP 3.1, SKARVIK OES 4). ONE POST PER APPROVAL ≠ EXACTLY-ONCE BUSINESS "
                              "EFFECT.", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/v1/openapi.json")
    app.state.services = svc

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> Response:
        return from_domain(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        fields = [".".join(str(x) for x in e.get("loc", ())) for e in exc.errors()][:20]
        return problem(request, ErrorCode.VALIDATION, "request validation failed", extra={"fields": fields})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = {401: ErrorCode.UNAUTHENTICATED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND}.get(
            exc.status_code, ErrorCode.VALIDATION)
        return problem(request, code, "", status=exc.status_code)

    @app.exception_handler(PoolTimeoutError)
    @app.exception_handler(DBAPIError)
    async def database_error(request: Request, exc: Exception) -> Response:
        if not database_unavailable(exc):
            raise exc  # an SQL failure stays an unexpected INTERNAL error
        log.error("database unavailable (%s)", exc.__class__.__name__,
                  extra={"request_id": getattr(request.state, "request_id", None)})
        return problem(request, ErrorCode.DATABASE_UNAVAILABLE, DATABASE_UNAVAILABLE_DETAIL,
                       extra={"title": "Database unavailable"})

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return problem(request, ErrorCode.INTERNAL, "")

    for r in (health.router, auth.router, customers.router, inventory.router, operations.router, queues.router,
              audit.router, admin.router, assistant.router):
        app.include_router(r, responses=None if r is health.router else _PROBLEM_503)
    if svc.config.profile in ("demo", "test"):
        from lbf.demo.router import router as demo_router

        app.include_router(demo_router, responses=_PROBLEM_503)
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def unknown_api(rest: str) -> Response:
        """Unknown API paths are 404 for every method (never swallowed by the SPA fallback)."""
        raise StarletteHTTPException(status_code=404)

    default_openapi = app.openapi

    def documented() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = openapi_doc.enrich(app, default_openapi())
        return app.openapi_schema

    app.openapi = documented  # type: ignore[method-assign]
    _mount_spa(app, svc.config.static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: Path | None) -> None:
    if static_dir is None or not (static_dir / "index.html").exists():
        return
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
    index = static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith(("api/", "healthz", "readyz", "metrics")):
            raise StarletteHTTPException(status_code=404)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and static_dir.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)
