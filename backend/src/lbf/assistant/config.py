"""Assistant configuration and the model rule (ADR-F10). Fail-closed for live
inference: double opt-in, key, the one authorized model and complete prices are all required; anything missing or any
other model ⇒ ``CONFIG_REJECTED`` with zero network calls."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

AUTHORIZED_OPENAI_MODEL = "gpt-6-luna"  # the only one; nothing in the environment or a record extends it (ADR-F10)
ASCII_WHITESPACE = " \t\n\r\v\f"


def _decimal(name: str) -> Decimal | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def read_key(env: dict[str, str] | None = None) -> str | None:
    """The key from ``OPENAI_API_KEY_FILE`` (Docker secret), else ``OPENAI_API_KEY`` (local development only)."""
    e = env if env is not None else dict(os.environ)
    path = e.get("OPENAI_API_KEY_FILE", "/run/secrets/openai_api_key")
    if path and Path(path).is_file():
        key = Path(path).read_text(encoding="utf-8").strip()
        if key:
            return key
    key = e.get("OPENAI_API_KEY", "").strip()
    return key or None


@dataclass(frozen=True)
class AssistantConfig:
    provider: str = "disabled"  # disabled | scripted | openai
    live_inference: bool = False
    model: str | None = None
    timeout_s: float = 30.0
    max_output_tokens: int = 1500
    max_retries: int = 1
    retry_max_wait_s: float = 10.0
    max_concurrency: int = 2
    max_requests_per_turn: int = 6
    input_price_per_mtok: Decimal | None = None
    cached_input_price_per_mtok: Decimal | None = None
    output_price_per_mtok: Decimal | None = None
    price_source: str | None = None
    price_observed_at: str | None = None
    max_request_cost_usd: Decimal = Decimal("0.20")
    max_turn_cost_usd: Decimal = Decimal("0.50")
    daily_budget_usd: Decimal = Decimal("2.00")
    key_present: bool = field(default=False, compare=False)
    model_explicit: bool = field(default=False, compare=False)  # OPENAI_MODEL was set; runs require it

    def __post_init__(self) -> None:
        """Outer ASCII whitespace is trimmed; blank means the authorized model, for the openai provider only."""
        model = self.model.strip(ASCII_WHITESPACE) if isinstance(self.model, str) else self.model
        if not model:
            model = AUTHORIZED_OPENAI_MODEL if self.provider == "openai" else None
        object.__setattr__(self, "model", model)

    @property
    def model_authorized(self) -> bool:
        return self.model == AUTHORIZED_OPENAI_MODEL

    @classmethod
    def from_env(cls) -> AssistantConfig:
        e = os.environ
        provider = e.get("LBF_ASSISTANT_PROVIDER", "disabled")
        return cls(
            provider=provider,
            live_inference=e.get("LBF_LIVE_INFERENCE", "false").lower() == "true",
            model=e.get("OPENAI_MODEL"),
            model_explicit=bool(e.get("OPENAI_MODEL", "").strip(ASCII_WHITESPACE)),
            timeout_s=float(e.get("LBF_OPENAI_TIMEOUT_SEC", "30")),
            max_output_tokens=min(8000, max(256, int(e.get("LBF_OPENAI_MAX_OUTPUT_TOKENS", "1500")))),
            max_retries=min(2, max(0, int(e.get("LBF_OPENAI_MAX_RETRIES", "1")))),
            retry_max_wait_s=float(e.get("LBF_OPENAI_RETRY_MAX_WAIT_SEC", "10")),
            max_concurrency=max(1, int(e.get("LBF_OPENAI_MAX_CONCURRENCY", "2"))),
            max_requests_per_turn=max(1, int(e.get("LBF_OPENAI_MAX_REQUESTS_PER_TURN", "6"))),
            input_price_per_mtok=_decimal("LBF_OPENAI_INPUT_PRICE_PER_MTOK"),
            cached_input_price_per_mtok=_decimal("LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK"),
            output_price_per_mtok=_decimal("LBF_OPENAI_OUTPUT_PRICE_PER_MTOK"),
            price_source=e.get("LBF_OPENAI_PRICE_SOURCE") or None,
            price_observed_at=e.get("LBF_OPENAI_PRICE_OBSERVED_AT") or None,
            max_request_cost_usd=_decimal("LBF_OPENAI_MAX_REQUEST_COST_USD") or Decimal("0.20"),
            max_turn_cost_usd=_decimal("LBF_OPENAI_MAX_TURN_COST_USD") or Decimal("0.50"),
            daily_budget_usd=_decimal("LBF_OPENAI_DAILY_BUDGET_USD") or Decimal("2.00"),
            key_present=provider == "openai" and read_key() is not None,  # not read while it is off
        )

    def live_problems(self) -> list[str]:
        """Non-secret names of every element missing or not authorized (empty ⇒ live inference permitted)."""
        missing = []
        if self.provider != "openai":
            missing.append("LBF_ASSISTANT_PROVIDER=openai")
        if not self.live_inference:
            missing.append("LBF_LIVE_INFERENCE=true")
        if not self.key_present:
            missing.append("OPENAI_API_KEY_FILE")
        if not self.model_authorized:
            missing.append("OPENAI_MODEL")
        for name, value in (("LBF_OPENAI_INPUT_PRICE_PER_MTOK", self.input_price_per_mtok),
                            ("LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK", self.cached_input_price_per_mtok),
                            ("LBF_OPENAI_OUTPUT_PRICE_PER_MTOK", self.output_price_per_mtok),
                            ("LBF_OPENAI_PRICE_SOURCE", self.price_source),
                            ("LBF_OPENAI_PRICE_OBSERVED_AT", self.price_observed_at)):
            if value is None:
                missing.append(name)
        return missing

    def digest(self) -> str:
        """Non-secret configuration digest (``ASSISTANT_CONFIG_LOADED``). The key itself never enters it."""
        data: dict[str, Any] = {k: str(v) for k, v in asdict(self).items() if k != "model_explicit"}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
