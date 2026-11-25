"""Shared helpers for the resilience-backed tests (R16–R18): observation recording and the standalone verifier."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "oracles" / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "oracles" / "src"))


def record_observation(scenario: str, variant: str, observations: dict[str, Any]) -> None:
    """Write observations for ``resilience/run.py`` when it set ``LBF_RES_OBS_DIR``; a no-op otherwise."""
    out = os.environ.get("LBF_RES_OBS_DIR")
    if out:
        path = Path(out) / f"{scenario}__{variant}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(observations, sort_keys=True, default=str, indent=1) + "\n")


def verifier_label(verdict: dict[str, Any]) -> str:
    """VALID / BROKEN (a specific event fails) / MANIFEST_MISMATCH (the file does not match its manifest)."""
    if verdict["verdict"] == "VALID":
        return "VALID"
    if verdict.get("first_broken_seq") is not None:
        return "BROKEN"
    return "MANIFEST_MISMATCH" if "manifest" in verdict.get("reason", "") else "INVALID"


def verify(jsonl: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    from lbf_oracles.audit_verify import verify_export

    return verify_export(jsonl, manifest).as_dict()
