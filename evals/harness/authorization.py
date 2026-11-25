"""Live-run guard and the model rule: a live evaluation starts only with
LBF_LIVE_INFERENCE=true, OPENAI_MODEL set explicitly to the one authorized model, and a valid, unexpired, complete
``governance/eval/AUTHORIZATION.json`` for the right purpose that names the same model."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lbf.assistant.config import ASCII_WHITESPACE, AUTHORIZED_OPENAI_MODEL
from lbf.assistant.smoke import RUN_ID, _dec

REPO = Path(__file__).resolve().parents[2]
COUNTS = ("max_requests", "max_trials", "max_input_tokens", "max_output_tokens", "max_concurrency")
AMOUNTS = ("max_estimated_usd", "max_request_cost_usd", "input_price_per_mtok", "cached_input_price_per_mtok",
           "output_price_per_mtok")
REQUIRED = ("run_id", "approved_by", "approved_at", "expires_at", "purpose", "provider", "model", *COUNTS, *AMOUNTS,
            "price_source_url", "price_observed_at")


def problem(auth: dict[str, Any], purpose: str, now: datetime | None = None) -> str:
    """Why this record does not authorize a run ("" when it does)."""
    missing = [k for k in REQUIRED if auth.get(k) in (None, "")]
    if missing:
        return "authorization incomplete: " + ", ".join(missing)
    if auth["purpose"] != purpose or auth["provider"] != "openai":
        return f"authorization is for purpose {auth['purpose']!r}, not {purpose!r}"
    if not isinstance(auth["run_id"], str) or not RUN_ID.fullmatch(auth["run_id"]):
        return "authorization run_id invalid"
    if auth["model"] != AUTHORIZED_OPENAI_MODEL \
            or os.environ.get("OPENAI_MODEL", "").strip(ASCII_WHITESPACE) != AUTHORIZED_OPENAI_MODEL:
        return "the authorization model and OPENAI_MODEL must both be the authorized model"
    bad = [k for k in COUNTS if type(auth[k]) is not int or auth[k] < 1]
    bad += [k for k in AMOUNTS if (_dec(auth[k]) or 0) <= 0]
    if bad:
        return "authorization values invalid: " + ", ".join(bad)
    if auth["max_concurrency"] != 1:
        return "an evaluation runs its trials one at a time"
    for key, variable in (("input_price_per_mtok", "LBF_OPENAI_INPUT_PRICE_PER_MTOK"),
                          ("cached_input_price_per_mtok", "LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK"),
                          ("output_price_per_mtok", "LBF_OPENAI_OUTPUT_PRICE_PER_MTOK")):
        if _dec(auth[key]) != _dec(os.environ.get(variable)):
            return f"authorization {key} differs from {variable}"
    if (_dec(auth["max_request_cost_usd"]) or 0) < (_dec(os.environ.get("LBF_OPENAI_MAX_REQUEST_COST_USD")) or
                                                    _dec("0.20")):
        return "LBF_OPENAI_MAX_REQUEST_COST_USD exceeds authorization max_request_cost_usd"
    try:
        start, end = (datetime.fromisoformat(str(auth[k]).replace("Z", "+00:00"))
                      for k in ("approved_at", "expires_at"))
        if not start <= (now or datetime.now(UTC)) < end:
            return "authorization expired or not yet valid"
    except (ValueError, TypeError):
        return "authorization dates unreadable"
    return ""


def check(purpose: str, now: datetime | None = None) -> tuple[dict[str, Any] | None, str]:
    """Returns (authorization, "") when a live run may start, else (None, reason)."""
    if os.environ.get("LBF_LIVE_INFERENCE", "false").lower() != "true":
        return None, "LBF_LIVE_INFERENCE is not true"
    path = Path(os.environ.get("LBF_EVAL_AUTHORIZATION", str(REPO / "governance/eval/AUTHORIZATION.json")))
    if not path.is_file():
        return None, f"no owner authorization file at {path}"
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "authorization file unreadable"
    if not isinstance(auth, dict):
        return None, "authorization file unreadable"
    reason = problem(auth, purpose, now)
    return (None, reason) if reason else (auth, "")
