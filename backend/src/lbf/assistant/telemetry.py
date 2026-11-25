"""Inference telemetry: one append-only ``inference_calls`` row per model request. No keys, no
headers, no raw prompts or outputs — only digests, usage as reported, and estimated cost."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from prometheus_client import Counter, Histogram
from sqlalchemy import Engine

from lbf.assistant.tables import inference_calls
from lbf.workflow.metrics import REGISTRY

REQUESTS = Counter("lbf_inference_requests_total", "Model requests", ["status", "error_category"], registry=REGISTRY)
LATENCY = Histogram("lbf_inference_latency_seconds", "Model request latency", registry=REGISTRY)
TOKENS = Counter("lbf_inference_tokens_total", "Tokens as reported by the provider", ["kind"], registry=REGISTRY)
COST = Counter("lbf_inference_estimated_cost_usd_total", "Estimated cost (USD, estimate)", registry=REGISTRY)
DENIALS = Counter("lbf_inference_budget_denials_total", "Requests not sent because of a budget", registry=REGISTRY)


@dataclass
class CallRecord:
    id: uuid.UUID
    provider: str
    started_at: datetime
    status: str
    correlation_id: uuid.UUID
    assistant_turn_id: uuid.UUID | None = None
    model_configured: str | None = None
    model_reported: str | None = None
    provider_request_id: str | None = None
    latency_ms: int | None = None
    error_category: str | None = None
    retry_no: int = 0
    usage_input_tokens: int | None = None
    usage_cached_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    reserved_usd: Decimal | None = None
    estimated_cost_usd: Decimal | None = None
    cost_basis: str | None = None
    request_sha256: str | None = None
    transport: str = "live"


class Telemetry:
    def __init__(self, engine: Engine | None, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def record(self, r: CallRecord) -> None:
        REQUESTS.labels(r.status, r.error_category or "").inc()
        if r.status == "BUDGET_DENIED":
            DENIALS.inc()
        if r.latency_ms is not None:
            LATENCY.observe(r.latency_ms / 1000)
        for kind, n in (("input", r.usage_input_tokens), ("cached_input", r.usage_cached_input_tokens),
                        ("output", r.usage_output_tokens)):
            if n:
                TOKENS.labels(kind).inc(n)
        if r.estimated_cost_usd:
            COST.inc(float(r.estimated_cost_usd))
        if self.engine is None:
            return
        with self.engine.begin() as conn:
            conn.execute(inference_calls.insert().values(**dict(vars(r))))
