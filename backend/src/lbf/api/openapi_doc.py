"""What the generated OpenAPI document adds to FastAPI's defaults: authentication, permissions and error responses.

Documentation only: nothing here is evaluated while serving a request. Authentication and permissions are read from the
routes' own dependencies, so they cannot drift; error responses are declared per operation and every response of the
API test suite is checked against them (``tests/api_support.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from lbf.api.deps import UNSAFE, authenticated
from lbf.auth import sessions
from lbf.domain.errors import HTTP_STATUS, ErrorCode

E = ErrorCode
PROBLEM = "application/problem+json"
BACKEND = (E.BACKEND_MALFORMED, E.BACKEND_AUTH)
STEP_UP = (E.REAUTH_REQUIRED,)
COMMAND = (E.NOT_FOUND, E.CONFLICT_STATE, E.CONFLICT_VERSION)

# Beyond what follows from the route itself (401/403 by dependency, 422 by parameters, 500, 503).
ERRORS: dict[str, tuple[ErrorCode, ...]] = {
    "login": (E.UNAUTHENTICATED, E.CSRF_FAILED, E.RATE_LIMITED),
    "reauthenticate": (E.RATE_LIMITED,),
    "searchCustomers": BACKEND, "getCustomer": (E.NOT_FOUND, *BACKEND), "getInventory": BACKEND, "dashboard": (),
    "getOperation": (E.NOT_FOUND,),
    "createDraft": (E.NOT_FOUND, *BACKEND),
    "reviseDraft": (*COMMAND, *BACKEND),
    "submitForApproval": (*COMMAND, *BACKEND),
    "approve": (*COMMAND, *STEP_UP, E.SOD_VIOLATION, E.CONFLICT_OVERLAP, E.APPROVAL_STALE_VALIDATION, *BACKEND),
    "decline": (*COMMAND, *STEP_UP, E.SOD_VIOLATION),
    "withdraw": COMMAND,
    "requestRecheck": (*COMMAND, E.RATE_LIMITED),
    "escalate": COMMAND,
    "resolve": (*COMMAND, *STEP_UP, E.SOD_VIOLATION, *BACKEND),
    "withdrawUnclaimed": (*COMMAND, *STEP_UP, E.CAPABILITY_UNSUPPORTED, E.SOD_VIOLATION),
    "authorizeRedelivery": (*COMMAND, *STEP_UP, E.CAPABILITY_UNSUPPORTED, E.SOD_VIOLATION),
    "assistantTurn": (E.RATE_LIMITED,),
    "createUser": (*STEP_UP, E.CONFLICT_STATE),
    "grantRole": (*STEP_UP, E.SOD_VIOLATION),
    "revokeRole": (*STEP_UP, E.SOD_VIOLATION, E.NOT_FOUND),
    "demoReset": (E.CONFLICT_STATE,),
}

NOTES: dict[str, str] = {
    "login":
        "Public entry: credentials plus an allowed `Origin` or `Referer`. No CSRF token is needed (none "
        "exists yet).",
    "getSession":
        "Also issues a fresh CSRF token; the previous one stops working.",
    "createDraft":
        "`Idempotency-Key` identifies the request, not its body. Repeating a key returns the first draft,"
        " its version and content hash with 201, also when the body differs; the new body is then "
        "ignored, so a 201 does not mean that this body was stored. Use a new key for every new intent and "
        "compare the returned `content_hash`. Keys are per user and are kept at least 24 hours. Without the "
        "header, every request creates a draft.",
    "submitForApproval":
        "A 200 with blocking checks leaves the order in DRAFT; read `state`.",
    "approve":
        "Binds the exact version and content hash. 200 means one authorization was created; the business "
        "outcome is established later by acknowledgement and read-back.",
    "authorizeRedelivery":
        "SKARVIK only, inside its deduplication window. 202 means authorized; the engine hands off the "
        "stored bytes once.",
    "withdrawUnclaimed":
        "SKARVIK only. NOT_APPLIED is reported only when non-delivery is proven.",
    "requestRecheck":
        "Read-only.",
    "resolve":
        "Verified resolution reads the named order; attested resolution records a statement and is never "
        "shown as verified.",
    "assistantTurn":
        "Reads and drafts with the caller's own permissions. `receipts` lists what the application "
        "stored, whatever the answer says: STORED was read back with its own audit event, UNVERIFIED was not, "
        "UNKNOWN was not observed. `proposal` was not executed; sending `confirm <code>` in the same session "
        "executes exactly it, at most once. Both fields are sent only with `LBF-Assistant-Version: 2`, and are "
        "absent when empty. Version 1, the default, has the eight required fields only and states the same "
        "facts in `notice`, with `answer.draft_operation_id` naming a written draft. Treat `mode` as an open "
        "set of strings.",
    "annotateInventory":
        "Write-only: the note is recorded in the audit trail and no operation returns it as inventory data.",
    "auditVerify":
        "Recomputes the hash chain.",
    "demoReset":
        "Mounted only in the demo and test profiles.",
    "demoArmFault":
        "Mounted only in the demo and test profiles.",
}


def _dependencies(dependant: Any) -> Iterator[Any]:
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependencies(dep)


def _problem_response(codes: Iterable[ErrorCode]) -> dict[str, Any]:
    values = sorted({c.value for c in codes})
    return {"description": "Problem (RFC 9457); `code` is one of " + ", ".join(values),
            "content": {PROBLEM: {"schema": {"allOf": [{"$ref": "#/components/schemas/Problem"}, {
                "type": "object", "properties": {"code": {"type": "string", "enum": values}}}]}}}}


def enrich(app: FastAPI, doc: dict[str, Any]) -> dict[str, Any]:
    components = doc.setdefault("components", {})
    components["securitySchemes"] = {
        "session": {"type": "apiKey", "in": "cookie", "name": sessions.COOKIE_NAME,
                    "description": "HttpOnly, SameSite=Strict session cookie set by login."},
        "csrf": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token",
                 "description": "Synchronizer token from login, reauth or getSession. State-changing requests also "
                                "need an allowed `Origin` (or `Referer`)."}}
    schemas = components.setdefault("schemas", {})
    schemas["Problem"] = {
        "title": "Problem", "type": "object", "description": "RFC 9457 problem details. `code` is stable.",
        "required": ["type", "title", "status", "code", "detail", "request_id"],
        "properties": {
            "type": {"type": "string"}, "title": {"type": "string"}, "status": {"type": "integer"},
            "code": {"type": "string", "enum": sorted(c.value for c in ErrorCode)}, "detail": {"type": "string"},
            "request_id": {"type": ["string", "null"]},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "Invalid fields (VALIDATION)."},
            "checks": {"type": "array", "items": {"type": "string"}, "description": "Policy checks that refused it."},
            "verdict": {"type": "string"}}}
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    for route in _api_routes(app):
        if not route.include_in_schema or route.operation_id is None:
            continue
        for method in route.methods or ():
            op = doc["paths"][route.path_format][method.lower()]
            _operation(route, method, op)
    return doc


def _api_routes(app: FastAPI) -> Iterator[APIRoute]:
    def walk(routes: Any) -> Iterator[APIRoute]:
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            else:
                inner = getattr(r, "original_router", None) or getattr(r, "router", None)
                if inner is not None:
                    yield from walk(inner.routes)

    yield from walk(app.routes)


def _operation(route: APIRoute, method: str, op: dict[str, Any]) -> None:
    name = str(route.operation_id)
    calls = list(_dependencies(route.dependant))
    session = authenticated in calls
    permissions = sorted({p.value for c in calls for p in getattr(c, "permissions", ())})
    codes: set[ErrorCode] = set(ERRORS.get(name, ()))
    if session:
        op["security"] = [{"session": [], "csrf": []}] if method in UNSAFE else [{"session": []}]
        codes.add(E.UNAUTHENTICATED)
        if method in UNSAFE:
            codes.add(E.CSRF_FAILED)
    else:
        op["security"] = []
    if permissions:
        op["x-permissions-any-of"] = permissions
        codes.add(E.FORBIDDEN)
    if E.REAUTH_REQUIRED in codes:
        op["x-step-up"] = True
    op["responses"].pop("422", None)
    if op.get("parameters") or op.get("requestBody"):
        codes.add(E.VALIDATION)
    if name in NOTES:
        op["description"] = NOTES[name]
    if name == "metrics":
        op["responses"]["200"] = {"description": "Prometheus text exposition",
                                  "content": {"text/plain": {"schema": {"type": "string"}}}}
        return
    if name == "readyz":
        report = {"application/json": {"schema": {"type": "object", "required": ["status", "db", "migrations"],
                                                  "additionalProperties": True}}}
        op["responses"]["200"] = {"description": "Ready, or degraded (see `status`)", "content": report}
        op["responses"]["503"] = {"description": "Not ready: the report names what is missing", "content": report}
        return
    if name == "healthz":
        return
    codes.add(E.INTERNAL)
    by_status: dict[int, set[ErrorCode]] = {}
    for code in codes:
        by_status.setdefault(HTTP_STATUS[code], set()).add(code)
    for status, group in sorted(by_status.items()):
        if status != 503:  # declared with its own schema in app.py
            op["responses"][str(status)] = _problem_response(group)
    if name in ("assistantStatus", "assistantTurn"):  # not an HTTP failure; only without an assistant
        op["responses"]["200"]["content"][PROBLEM] = _problem_response([E.ASSISTANT_UNAVAILABLE])["content"][PROBLEM]
