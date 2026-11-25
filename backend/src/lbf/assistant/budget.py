"""Inference budgets and the append-only ledger. Enforced BEFORE every request.

``reserve_usd = input_bytes × input_price / 1e6 + max_output_tokens × output_price / 1e6`` (bytes are a deliberately
conservative upper bound on tokens; no tokenizer). Budget used = Σ RESERVE not yet settled + Σ SETTLE, per scope.
Reported money is always an **estimate**.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import Engine, func, select, text

from lbf.assistant.config import AssistantConfig
from lbf.assistant.tables import inference_budget_ledger as L

MTOK = Decimal(1_000_000)
Scope = Literal["TURN", "DAILY", "EVAL"]


@dataclass(frozen=True)
class Reservation:
    call_id: uuid.UUID
    amount: Decimal
    scopes: tuple[tuple[Scope, str], ...]


@dataclass(frozen=True)
class Denial:
    reason: str  # REQUEST_CEILING | TURN_BUDGET | DAILY_BUDGET | EVAL_BUDGET


class BudgetService:
    def __init__(self, engine: Engine, config: AssistantConfig, clock: Callable[[], datetime],
                 eval_budget_usd: Decimal | None = None) -> None:
        self.engine = engine
        self.config = config
        self.clock = clock
        self.eval_budget_usd = eval_budget_usd

    def worst_case(self, input_bytes: int) -> Decimal:
        c = self.config
        assert c.input_price_per_mtok is not None and c.output_price_per_mtok is not None
        return (Decimal(input_bytes) * c.input_price_per_mtok / MTOK
                + Decimal(c.max_output_tokens) * c.output_price_per_mtok / MTOK).quantize(Decimal("0.000001"))

    def settle_amount(self, input_tokens: int | None, cached: int | None, output_tokens: int | None,
                      reserved: Decimal) -> tuple[Decimal, str]:
        c = self.config
        if input_tokens is None or output_tokens is None:
            return reserved, "WORST_CASE_RESERVATION"
        cached = cached or 0
        assert c.input_price_per_mtok is not None and c.output_price_per_mtok is not None
        cached_price = c.cached_input_price_per_mtok if c.cached_input_price_per_mtok is not None \
            else c.input_price_per_mtok
        cost = (Decimal(max(0, input_tokens - cached)) * c.input_price_per_mtok + Decimal(cached) * cached_price
                + Decimal(output_tokens) * c.output_price_per_mtok) / MTOK
        return cost.quantize(Decimal("0.000001")), "REPORTED_USAGE"

    def _used(self, conn: object, scope: Scope, key: str) -> Decimal:
        c = conn  # sqlalchemy Connection
        settles = c.execute(select(func.coalesce(func.sum(L.c.amount_usd), 0)).where(  # type: ignore[attr-defined]
            L.c.scope == scope, L.c.scope_key == key, L.c.entry == "SETTLE")).scalar()
        settled_ids = select(L.c.inference_call_id).where(L.c.scope == scope, L.c.scope_key == key,
                                                          L.c.entry == "SETTLE")
        open_res = c.execute(select(func.coalesce(func.sum(L.c.amount_usd), 0)).where(  # type: ignore[attr-defined]
            L.c.scope == scope, L.c.scope_key == key, L.c.entry == "RESERVE",
            L.c.inference_call_id.not_in(settled_ids))).scalar()
        return Decimal(settles) + Decimal(open_res)

    def reserve(self, input_bytes: int, *, turn_key: str | None, eval_key: str | None) -> Reservation | Denial:
        amount = self.worst_case(input_bytes)
        if amount > self.config.max_request_cost_usd:
            return Denial("REQUEST_CEILING")
        day = self.clock().date().isoformat()
        checks: list[tuple[Scope, str, Decimal, str]] = []
        if eval_key is not None:
            checks.append(("EVAL", eval_key, self.eval_budget_usd or Decimal(0), "EVAL_BUDGET"))
        else:
            if turn_key is not None:
                checks.append(("TURN", turn_key, self.config.max_turn_cost_usd, "TURN_BUDGET"))
            checks.append(("DAILY", day, self.config.daily_budget_usd, "DAILY_BUDGET"))
        call_id = uuid.uuid4()
        with self.engine.begin() as conn:
            for scope, key, _, _ in checks:  # serialize reservations per scope key
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"budget:{scope}:{key}"})
            for scope, key, limit, reason in checks:
                if self._used(conn, scope, key) + amount > limit:
                    return Denial(reason)
            now = self.clock()
            for scope, key, _, _ in checks:
                conn.execute(L.insert().values(id=uuid.uuid4(), scope=scope, scope_key=key, entry="RESERVE",
                                               inference_call_id=call_id, amount_usd=amount, created_at=now))
        return Reservation(call_id, amount, tuple((s, k) for s, k, _, _ in checks))

    def settle(self, reservation: Reservation, amount: Decimal, basis: str) -> None:
        with self.engine.begin() as conn:
            for scope, key in reservation.scopes:
                conn.execute(L.insert().values(id=uuid.uuid4(), scope=scope, scope_key=key, entry="SETTLE",
                                               inference_call_id=reservation.call_id, amount_usd=amount,
                                               cost_basis=basis, created_at=self.clock()))
