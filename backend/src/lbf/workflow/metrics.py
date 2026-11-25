"""Prometheus metrics (OPS-02). A dedicated registry; the API exposes it at ``/metrics``."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

REGISTRY = CollectorRegistry(auto_describe=True)

TRANSITIONS = Counter("lbf_operation_transitions_total", "Operation state transitions recorded",
                      ["to_state"], registry=REGISTRY)
TRANSMISSIONS = Counter("lbf_transmissions_total", "Transport handoffs by kind and phase-1 observation",
                        ["kind", "observation"], registry=REGISTRY)
ACKS = Counter("lbf_acknowledgements_total", "Acknowledgement files recorded", ["status", "matched"],
               registry=REGISTRY)
RECONCILIATIONS = Counter("lbf_reconciliation_runs_total", "Reconciliation runs by trigger and verdict",
                          ["trigger", "verdict"], registry=REGISTRY)
ENGINE_LEADER = Gauge("lbf_engine_leader", "1 when this process holds the singleton engine lease",
                      registry=REGISTRY)
