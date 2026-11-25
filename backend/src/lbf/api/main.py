"""Production entry point: ``python -m lbf.api.main`` (uvicorn, ONE worker — ADR-F04)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from lbf.adapters.halden.adapter import HaldenAdapter
from lbf.adapters.skarvik.adapter import SkarvikAdapter
from lbf.api.app import create_app
from lbf.api.services import AppConfig, AppServices
from lbf.auth.service import AuthService
from lbf.engines.pause import PausePoints
from lbf.engines.runner import EngineRunner
from lbf.persistence.db import make_engine
from lbf.persistence.settings import DbSettings, read_secret
from lbf.ports.backend import LegacyBackendPort
from lbf.ports.capabilities import skarvik_capabilities
from lbf.workflow.context import WorkflowContext, WorkflowSettings, load_mappings


def build() -> tuple[AppServices, bool]:
    config = AppConfig.from_env()
    db = DbSettings.from_env()
    engine = make_engine(db, "app")
    audit_engine = make_engine(db, "audit_reader", pool_size=2) if db.passwords.get("audit_reader") else None
    with engine.connect() as conn:
        mappings = load_mappings(conn)
    settings = WorkflowSettings.from_env()
    test_timings = config.profile == "test"
    ports: dict[str, LegacyBackendPort] = {
        "halden": HaldenAdapter(config.halden_url, os.environ.get("LBF_HALDEN_LOGIN", "svc_bridge"),
                                read_secret("LBF_HALDEN_SECRET", "/run/secrets/halden_secret") or "",
                                mappings=mappings, timeout=settings.transport_timeout_seconds),
        "skarvik": SkarvikAdapter(Path(os.environ.get("LBF_SKARVIK_EXCHANGE_DIR", "/exchange")), mappings=mappings,
                                  capabilities=skarvik_capabilities(test_profile=test_timings)),
    }
    workflow = WorkflowContext(engine=engine, backends=ports, mappings=mappings, settings=settings)
    svc = AppServices(config=config, engine=engine, audit_engine=audit_engine, auth=AuthService(engine),
                      workflow=workflow)
    if config.engines_enabled:
        svc.runner = EngineRunner(workflow, pauses=PausePoints.from_env())
    from lbf.assistant.factory import build_assistant  # integration point

    svc.extras["assistant"] = build_assistant(svc)
    return svc, config.engines_enabled


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s",'
                                                   '"logger":"%(name)s","msg":"%(message)s"}')
    svc, engines = build()
    app = create_app(svc, start_engines=engines)
    uvicorn.run(app, host=os.environ.get("LBF_BIND", "0.0.0.0"), port=int(os.environ.get("LBF_PORT", "8080")),  # noqa: S104
                workers=1, proxy_headers=False, server_header=False, log_config=None)


if __name__ == "__main__":
    main()
