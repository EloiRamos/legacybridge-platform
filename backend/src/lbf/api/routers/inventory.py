"""Inventory visibility with explicit uncertainty (BR-02): each backend listed separately (I-3, never summed)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from lbf.api.deps import Auth, require, services
from lbf.api.schemas import AnnotationIn, AvailabilityOut, InventoryItemOut, InventoryOut
from lbf.api.services import AppServices
from lbf.domain.enums import Permission
from lbf.domain.errors import DomainError
from lbf.domain.models import Availability
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow.records import user_event

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


def availability_out(a: Availability) -> AvailabilityOut:
    return AvailabilityOut(backend_id=a.backend_id, native_item=a.native_item, canonical_sku=a.canonical_sku,
                           status=a.status, available_base=a.available_base, as_of=a.as_of, freshness=a.freshness,
                           snapshot_age_seconds=a.snapshot_age_seconds, explanation=a.explanation,
                           issues=[i.model_dump() for i in a.issues], warehouses=[w.model_dump() for w in a.warehouses],
                           legacy_available_flag=a.legacy_available_flag)


def inventory(svc: AppServices) -> InventoryOut:
    items: dict[str, InventoryItemOut] = {}
    unmapped, errors = [], {}
    names: dict[str, str] = {}
    for backend_id, port in svc.workflow.backends.items():
        try:
            for e in port.catalogue().entries:
                if e.canonical is not None:
                    names.setdefault(e.canonical.canonical_sku, e.name)
            snapshot = port.stock()
        except DomainError as exc:
            errors[backend_id] = exc.message
            continue
        for a in snapshot.availability:
            if a.canonical_sku is None:
                unmapped.append(availability_out(a))
                continue
            item = items.setdefault(a.canonical_sku, InventoryItemOut(canonical_sku=a.canonical_sku,
                                                                      name=names.get(a.canonical_sku, a.canonical_sku),
                                                                      per_backend=[]))
            item.per_backend.append(availability_out(a))
    return InventoryOut(items=sorted(items.values(), key=lambda i: i.canonical_sku), unmapped=unmapped,
                        backend_errors=errors)


@router.get("", response_model=InventoryOut, operation_id="getInventory")
def get_inventory(_: Auth = Depends(require(Permission.INVENTORY_READ)),
                  svc: AppServices = Depends(services)) -> InventoryOut:
    return inventory(svc)


@router.post("/annotations", status_code=201, operation_id="annotateInventory")
def annotate(body: AnnotationIn, auth: Auth = Depends(require(Permission.INVENTORY_ANNOTATE)),
             svc: AppServices = Depends(services)) -> dict[str, str]:
    annotation_id = uuid.uuid4()
    with UnitOfWork(svc.engine, clock=svc.clock) as uow:
        annotations = t.metadata.tables["lbf.annotations"]  # (``t.annotations`` shadows a __future__ name)
        uow.conn.execute(annotations.insert().values(
            id=annotation_id, canonical_sku=body.canonical_sku, backend_id=body.backend_id,
            native_item=body.native_item, author=auth.principal.user_id, body=body.body, created_at=uow.clock()))
        uow.append_event(user_event(auth.principal, "INVENTORY_ANNOTATED",
                                    payload={"canonical_sku": body.canonical_sku, "native_item": body.native_item}))
    return {"annotation_id": str(annotation_id)}
