"""Liveness, readiness (DB, migrations at head, backends, engine leader), Prometheus metrics (OPS-02)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select, text

from lbf.api.deps import services
from lbf.api.services import AppServices
from lbf.persistence import tables as t
from lbf.workflow import metrics

router = APIRouter(tags=["health"])
# The Alembic head this code is built for. The application image does not contain the migration scripts (they run from
# the migrate image), so the head is recorded here;
# tests/api/test_create_races_and_outage.py fails if it drifts from them.
EXPECTED_MIGRATION_HEAD = "0004_inference"


def backend_health(svc: AppServices) -> list[dict[str, Any]]:
    out = []
    for backend_id, port in svc.workflow.backends.items():
        try:
            h = port.health()
            out.append({"backend_id": backend_id, "reachable": h.reachable, "detail": h.detail,
                        "snapshot_age_seconds": h.snapshot_age_seconds,
                        "freshness": port.capabilities().inventory_freshness})
        except Exception as exc:  # health is informational; never raise
            out.append({"backend_id": backend_id, "reachable": False, "detail": exc.__class__.__name__})
    return out


@router.get("/healthz", operation_id="healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", operation_id="readyz")
def readyz(svc: AppServices = Depends(services)) -> JSONResponse:
    report: dict[str, Any] = {"db": "unavailable", "migrations": "unknown"}
    try:
        with svc.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            report["db"] = "ok"
            revisions = conn.execute(text("SELECT version_num FROM lbf.alembic_version")).scalars().all()
        report["migrations"] = "head" if list(revisions) == [EXPECTED_MIGRATION_HEAD] else "not-head"
    except Exception:  # noqa: S110 - unreachable database or unreadable revision: reported as not ready below
        pass
    report["backends"] = backend_health(svc)
    leader = bool(svc.runner is not None and svc.runner.leader)
    report["engine"] = "leader" if leader else ("disabled" if not svc.config.engines_enabled
                                                else "not-engine-leader")
    report["assistant"] = svc.extras.get("assistant_status", svc.config.assistant_provider)
    if getattr(getattr(svc.extras.get("assistant"), "provider", None), "stopped", False):
        report["assistant"] = "stopped:model-mismatch"  # informational: the application stays ready without it
    backends_ok = all(b["reachable"] for b in report["backends"])
    if report["db"] != "ok" or report["migrations"] != "head" or not backends_ok:
        report["status"] = "not-ready"
        return JSONResponse(report, status_code=503)
    report["status"] = "ready" if leader or not svc.config.engines_enabled else "degraded:not-engine-leader"
    return JSONResponse(report, status_code=200)


class _ScrapeCollector(Collector):
    def __init__(self, svc: AppServices) -> None:
        self.svc = svc

    def collect(self) -> Any:
        by_state = GaugeMetricFamily("lbf_operations", "Operations by state", labels=["state"])
        age = GaugeMetricFamily("lbf_unknown_age_max_seconds", "Age of the oldest UNKNOWN operation")
        health = GaugeMetricFamily("lbf_backend_up", "Backend reachable (1/0)", labels=["backend"])
        try:
            with self.svc.engine.connect() as conn:
                for state, n in conn.execute(select(t.operations.c.state, func.count())
                                             .group_by(t.operations.c.state)).all():
                    by_state.add_metric([state], n)
                oldest = conn.execute(select(func.min(t.operations.c.unknown_since))
                                      .where(t.operations.c.state == "UNKNOWN")).scalar()
            age.add_metric([], (self.svc.clock() - oldest).total_seconds() if oldest else 0.0)
        except Exception:  # noqa: S110 - metrics never fail the scrape
            pass
        for b in backend_health(self.svc):
            health.add_metric([b["backend_id"]], 1.0 if b["reachable"] else 0.0)
        yield from (by_state, age, health)


@router.get("/metrics", operation_id="metrics")
def metrics_endpoint(svc: AppServices = Depends(services)) -> Response:
    from prometheus_client import CollectorRegistry

    scrape = CollectorRegistry()
    scrape.register(_ScrapeCollector(svc))
    body = generate_latest(metrics.REGISTRY) + generate_latest(scrape)
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")
