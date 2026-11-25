"""Unified customer lookup (BR-01): accounts per backend, ambiguity groups (C-1), parties only from the curated
crosswalk (C-2). Free-text backend fields are returned as untrusted text."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from lbf.api import readmodels
from lbf.api.deps import Auth, require, services
from lbf.api.schemas import AccountOut, CustomerCardOut, CustomerSearchOut, PartyOut
from lbf.api.services import AppServices
from lbf.domain.customers import search_result
from lbf.domain.enums import OVERLAP_BLOCKING_STATES, Permission
from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.models import BackendAccount
from lbf.persistence import tables as t
from lbf.workflow.context import linked_accounts

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


def account_out(a: BackendAccount) -> AccountOut:
    untrusted = {"notes": a.notes} if a.notes else {}
    return AccountOut(backend_id=a.ref.backend_id, native_id=a.ref.native_id, name_raw=a.name_raw,
                      name_display=a.name_display, credit_hold=a.credit_hold, city=a.city,
                      country_code=a.country_code, contact_name=a.contact_name, email=a.email, phone=a.phone,
                      customer_since=a.customer_since, issues=[i.model_dump() for i in a.issues],
                      untrusted_text=untrusted)


def crosswalk(svc: AppServices) -> tuple[dict[tuple[str, str], tuple[uuid.UUID, str]], dict[uuid.UUID, list[Any]]]:
    with svc.engine.connect() as conn:
        rows = conn.execute(select(t.party_accounts.c.party_id, t.party_accounts.c.backend_id,
                                   t.party_accounts.c.native_customer_id, t.parties.c.display_name)
                            .join(t.parties, t.parties.c.id == t.party_accounts.c.party_id)).all()
    cw = {(r.backend_id, r.native_customer_id): (r.party_id, r.display_name) for r in rows}
    members: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for r in rows:
        members.setdefault(r.party_id, []).append((r.backend_id, r.native_customer_id))
    return cw, members


def search(svc: AppServices, q: str) -> CustomerSearchOut:
    accounts, errors = [], {}
    for backend_id, port in svc.workflow.backends.items():
        try:
            accounts.extend(port.search_customers(q))
        except DomainError as exc:
            errors[backend_id] = exc.message
    cw, member_refs = crosswalk(svc)
    hit_parties = {cw[(a.ref.backend_id, a.ref.native_id)][0] for a in accounts
                   if (a.ref.backend_id, a.ref.native_id) in cw}
    party_members: dict[uuid.UUID, list[BackendAccount]] = {}
    for pid in hit_parties:
        resolved = []
        for backend_id, native in member_refs[pid]:
            found = next((a for a in accounts if (a.ref.backend_id, a.ref.native_id) == (backend_id, native)), None)
            if found is None:
                try:
                    found = svc.workflow.port(backend_id).get_customer(native)
                except DomainError:
                    continue
            resolved.append(found)
        party_members[pid] = resolved
    result = search_result(accounts, cw, party_members)
    return CustomerSearchOut(
        query=q, accounts=[account_out(a) for a in result.accounts],
        parties=[PartyOut(party_id=p.party_id, display_name=p.display_name,
                          accounts=[account_out(a) for a in p.accounts],
                          conflicts=[c.model_dump() for c in p.conflicts]) for p in result.parties],
        ambiguity_groups=[[f"{r.backend_id}:{r.native_id}" for r in g] for g in result.ambiguity_groups],
        backend_errors=errors)


@router.get("", response_model=CustomerSearchOut, operation_id="searchCustomers")
def search_customers(q: str = Query(min_length=2, max_length=60), _: Auth = Depends(require(Permission.CUSTOMER_READ)),
                     svc: AppServices = Depends(services)) -> CustomerSearchOut:
    return search(svc, q)


@router.get("/{backend_id}/{native_id}", response_model=CustomerCardOut, operation_id="getCustomer")
def get_customer(backend_id: str, native_id: str, _: Auth = Depends(require(Permission.CUSTOMER_READ)),
                 svc: AppServices = Depends(services)) -> CustomerCardOut:
    port = svc.workflow.port(backend_id)
    account = port.get_customer(native_id)
    errors: dict[str, str] = {}
    try:
        orders = [o.model_dump(mode="json") for o in port.list_orders(native_id)][-10:]
    except DomainError as exc:
        orders, errors[backend_id] = [], exc.message
    linked = []
    with svc.engine.connect() as conn:
        links = linked_accounts(conn, backend_id, account.ref.native_id)
        ops = readmodels.list_operations(conn, backend=backend_id, customer=account.ref.native_id,
                                         states=[s.value for s in OVERLAP_BLOCKING_STATES] + ["DRAFT", "READY"])
    for b, n in links:
        try:
            linked.append(account_out(svc.workflow.port(b).get_customer(n)))
        except DomainError as exc:
            errors[b] = exc.message
    if account is None:
        raise DomainError(ErrorCode.NOT_FOUND, "customer not found")
    return CustomerCardOut(account=account_out(account), linked_accounts=linked, orders=orders,
                           open_operations=ops, backend_errors=errors)
