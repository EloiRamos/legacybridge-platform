"""``python -m evals.run --mode scripted|baseline|live --out DIR``.

Exit codes: 0 complete with hard safety 100 %; 1 a hard-safety violation; 2 live run refused (no authorization;
zero network calls); 3 evaluation budget exhausted (partial report ``INCOMPLETE_BUDGET_EXHAUSTED``); 4 the provider did
not report the authorized model (``ABORTED_MODEL_MISMATCH``) or the run stopped itself (``ABORTED_UNSAFE``).
Run from ``backend/`` with ``PYTHONPATH=..`` so ``lbf`` and ``evals`` are importable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from evals.harness.authorization import check, problem
from evals.harness.judge import judge
from evals.oracles import db
from lbf.assistant.smoke import ABORT_EXIT, ABORTED, CappedProvider
from lbf.assistant.smoke_ledger import Ledger, LedgerError

TASKS = Path(__file__).resolve().parent / "tasks"


def load_tasks(pattern: str = "*.yaml") -> list[dict[str, Any]]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(TASKS.glob(pattern))]


def substitute(value: Any, fixtures: dict[str, str]) -> Any:
    if isinstance(value, str):
        for k, v in fixtures.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [substitute(v, fixtures) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, fixtures) for k, v in value.items()}
    return value


def run(mode: str, out: Path, *, tasks: list[dict[str, Any]] | None = None,
        provider_factory: Callable[[Any, dict[str, Any]], Any] | None = None,
        authorization: dict[str, Any] | None = None, ledger_path: Path | None = None, env: Any = None) -> int:
    """A live run needs its complete authorization and its own ledger; it starts once and never resumes. The factory
    returns the provider, which every trial shares through the request cap and the model guard."""
    ledger: Ledger | None = None
    invocation = ""
    if mode == "live":
        refused = problem(authorization, "evaluation") if authorization else "a live run needs its authorization"
        if not refused and (ledger_path is None or provider_factory is None):
            refused = "a live run needs its authorization ledger and provider"
        if not refused and authorization and ledger_path:
            try:
                ledger = Ledger(ledger_path)
                refused = ledger.bind(authorization) or ""
                if not refused:
                    invocation = ledger.start_invocation(authorization["run_id"], only=True)
            except LedgerError as exc:
                refused = str(exc)
        if refused:
            print(json.dumps({"status": "REFUSED", "reason": refused}))
            return 2
    out.mkdir(parents=True, exist_ok=True)
    from evals.harness.env import EvalEnv
    from lbf.assistant.config import AssistantConfig
    from lbf.assistant.providers.disabled import DisabledProvider
    from lbf.assistant.providers.scripted import ScriptedProvider
    from lbf.assistant.service import AssistantService

    own_env = env is None
    env = env or EvalEnv().build()
    run_id = f"eval-{mode}-{uuid.uuid4().hex[:8]}"
    live: CappedProvider | None = None
    if ledger is not None and authorization is not None and provider_factory is not None:
        run_id = str(authorization["run_id"])
        inner = provider_factory(env, authorization)
        live = CappedProvider(inner, authorization["max_requests"], ledger, run_id, invocation)
    results: list[dict[str, Any]] = []
    status = "COMPLETE"
    selected = tasks if tasks is not None else load_tasks()
    for task in selected[:authorization["max_trials"]] if live is not None and authorization else selected:
        task = substitute(task, env.fixtures)
        if mode == "scripted":
            assistant = AssistantService(env.svc, ScriptedProvider(task["scripted"]),
                                         AssistantConfig(provider="scripted"))
            prompt = task["prompt"]
        elif mode == "baseline":
            assistant = AssistantService(env.svc, DisabledProvider(), AssistantConfig())
            prompt = task["baseline"]
        else:
            assert live is not None
            live.scenario = str(task["id"])
            assistant = AssistantService(env.svc, live, live.inner.config)
            prompt = task["prompt"]
        before = db.counts(env.oracle_dsn)
        t0 = time.monotonic()
        o = assistant.run_turn(env.principal(task["user"]), prompt,
                               eval_run_id=run_id if mode == "live" else None)
        latency = int((time.monotonic() - t0) * 1000)
        after = db.counts(env.oracle_dsn)
        new_ops = db.new_operations(env.oracle_dsn, before["operations"])
        outcome = {"answer": o.answer.model_dump(mode="json"), "fallback_used": o.fallback_used,
                   "fallback_reason": o.fallback_reason, "tool_calls": o.tool_calls, "refused_calls": o.refused_calls}
        verdict = judge(task, outcome, before, after, new_ops, env.fixtures, env.oracle_dsn)
        called = {c["name"] for c in o.tool_calls if c["permitted"]}
        results.append({
            "id": task["id"], "class": task["class"], "success": verdict.success, "hard_safety": verdict.hard_safety,
            "notes": verdict.notes, "latency_ms": latency, "fallback_reason": o.fallback_reason,
            "tool_selection_ok": not task.get("tools_any") or bool(called & set(task["tools_any"])),
            "unauthorized_attempts": sum(1 for r in o.refused_calls if r["reason"] in ("UNKNOWN_TOOL", "FORBIDDEN")),
            "grounding_failed": o.fallback_reason == "GROUNDING_FAILED",
            "answer_sha256": hashlib.sha256(json.dumps(outcome, sort_keys=True).encode()).hexdigest()})
        if live is not None and live.guard.closed:
            status = ABORTED
            break
        if mode == "live" and not verdict.hard_safety:
            status = "ABORTED_UNSAFE"
            break
        if o.fallback_reason == "BUDGET_DENIED" and mode == "live":
            status = "INCOMPLETE_BUDGET_EXHAUSTED"
            break
    if own_env:
        env.close()
    summary = summarize(mode, run_id, status, results)
    if live is not None and ledger is not None:
        summary |= {"model_configured": live.inner.config.model, "observed_version": live.guard.observed,
                    "model_reported": [r.get("model_reported") for r in live.log], "requests": live.log}
        live.finish(status)
        ledger.close()
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text(report_md(summary), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("status", "tasks", "success", "hard_safety_passed")}))
    if status == "INCOMPLETE_BUDGET_EXHAUSTED":
        return 3
    if status.startswith("ABORTED"):
        return ABORT_EXIT
    return 0 if summary["hard_safety_passed"] == summary["tasks"] else 1


def summarize(mode: str, run_id: str, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    lat = sorted(r["latency_ms"] for r in results) or [0]
    by_class: dict[str, dict[str, int]] = {}
    for r in results:
        c = by_class.setdefault(r["class"], {"tasks": 0, "success": 0})
        c["tasks"] += 1
        c["success"] += int(r["success"])
    return {
        "schema": "lbf-eval/1.0", "mode": mode, "run_id": run_id, "status": status, "tasks": len(results),
        "success": sum(r["success"] for r in results), "pass_k": 1,
        "hard_safety_passed": sum(r["hard_safety"] for r in results),
        "incorrect_tool_selection": sum(not r["tool_selection_ok"] for r in results),
        "unauthorized_action_attempts": sum(r["unauthorized_attempts"] for r in results),
        "incorrect_customer_identification": sum(not r["success"] for r in results
                                                 if r["class"] == "customer_disambiguation"),
        "inventory_interpretation_errors": sum(not r["success"] for r in results
                                               if r["class"] == "inventory_interpretation"),
        "hallucinated_operational_facts": sum(r["grounding_failed"] for r in results),
        "latency_ms_p50": statistics.median(lat), "latency_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))],
        "tokens_reported": "not applicable (no model)" if mode != "live" else "see inference_calls",
        "estimated_cost_usd": "0 (no paid call)" if mode != "live" else "see ledger (estimate)",
        "by_class": by_class, "results": results,
    }


def report_md(s: dict[str, Any]) -> str:
    rows = "\n".join(f"| {c} | {v['tasks']} | {v['success']} |" for c, v in sorted(s["by_class"].items()))
    fails = "\n".join(f"- {r['id']} ({r['class']}): {'; '.join(r['notes']) or 'failed'}"
                      for r in s["results"] if not r["success"]) or "- none"
    head = (f"| {s['status']} | {s['tasks']} | {s['success']} | {s['hard_safety_passed']}/{s['tasks']} "
            f"| {s['unauthorized_action_attempts']} | {s['hallucinated_operational_facts']} |")
    return "\n".join([
        f"# Assistant evaluation — {s['mode']} ({s['run_id']})", "",
        f"Fictional data; simulated backends; no paid inference ({s['estimated_cost_usd']}). Author-executed.", "",
        "| Status | Tasks | Success (oracle) | Hard safety | Unauthorized attempts (refused) | Grounding failures |",
        "|---|---|---|---|---|---|", head, "",
        f"Latency p50/p95: {s['latency_ms_p50']} / {s['latency_ms_p95']} ms. "
        f"Incorrect tool selection: {s['incorrect_tool_selection']}. "
        f"Incorrect customer identification: {s['incorrect_customer_identification']}. "
        f"Inventory interpretation errors: {s['inventory_interpretation_errors']}.", "",
        "| Class | Tasks | Success |", "|---|---|---|", rows, "", "## Failed tasks", fails, ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LBF assistant evaluation")
    parser.add_argument("--mode", choices=["scripted", "baseline", "live"], required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tasks", default="*.yaml")
    args = parser.parse_args(argv)
    if args.mode == "live":
        auth, reason = check("evaluation")
        if auth is None:
            print(json.dumps({"status": "REFUSED", "reason": reason}))
            return 2
        print(json.dumps({"status": "REFUSED", "reason": "a live run is executed by "
                          "evals.live_campaign, with its ledgers"}))
        return 2
    return run(args.mode, Path(args.out), tasks=load_tasks(args.tasks))


if __name__ == "__main__":
    sys.exit(main())
