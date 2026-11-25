"""Resilience runner.

    cd backend && PYTHONPATH=.. uv run --frozen python -m resilience.run --scenario all --seed 20261121 \
        --out ../acceptance/evidence/<run-id>/resilience/run1

Runs R01–R18 sequentially against the running ``test`` profile (``make test-up``). Container-level variants drive
the stack over HTTP (``resilience/scenarios/live.py``); in-process variants run as pytest modules that write their
observations (``backend/tests/resilience``, ``backend/tests/security/test_audit_tamper.py``). Observations are
compared with the versioned expectations ``resilience/expected/R<nn>.yaml``.

Outputs: ``R<nn>.json`` (inputs, observations, comparison, verdict), ``summary.json``, ``outcomes.tsv`` (normalized:
no ids or timestamps — the determinism gate compares it byte for byte across two runs), ``audit/`` (one export +
manifest per scenario segment, standalone-verifier output ``verify.txt``, reconstructions) and ``audit/compare.json``
(E-04: the application's own account of handoffs per correlation value versus the wire-tap ledger and SKARVIK's
own ledgers). Exit 0 only when every scenario passes and E-01/E-04 hold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from resilience.harness.api import Api
from resilience.harness.stack import EXCHANGE, REPO, Stack
from resilience.scenarios.live import LIVE, Ctx

BACKEND = REPO / "backend"
EXPECTED = REPO / "resilience" / "expected"
ORDER = [f"R{n:02d}" for n in range(1, 19)]
INPROCESS = {  # scenario -> pytest node ids (run from backend/)
    "R07": ["tests/resilience/test_r07_r08_concurrency.py::test_r07_two_api_processes_one_approval_per_round"],
    "R08": ["tests/resilience/test_r07_r08_concurrency.py::test_r08_two_engines_claim_simultaneously"],
    "R16": ["tests/resilience/test_r16_matrix.py"],
    "R17": ["tests/resilience/test_r17_injection.py"],
    "R18": ["tests/resilience/test_r18_audit_failure.py", "tests/security/test_audit_tamper.py"],
}
DESCRIPTIVE = {"then"}  # prose in the expectation, not an observable
RECONSTRUCT = {"R03", "R11", "R14"}  # V7-3: --reconstruct for these scenarios' operations


# ----------------------------------------------------------------------------- comparison


def compare(expected: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, want in expected.items():
        if key in DESCRIPTIVE:
            continue
        if key not in observed:
            rows.append({"key": key, "expected": want, "observed": "<not observed>", "ok": False, "rule": "eq"})
            continue
        got = observed[key]
        if key == "path":  # the expected states appear in this order among the states entered
            it = iter(got)
            ok = all(any(s == x for x in it) for s in want)
            rows.append({"key": key, "expected": want, "observed": "contains in order" if ok else got, "ok": ok,
                         "rule": "subsequence"})
        elif key == "events":
            ok = set(want) <= set(got)
            rows.append({"key": key, "expected": want, "observed": "contains" if ok else got, "ok": ok,
                         "rule": "subset"})
        elif key == "per_round":
            for k, v in want.items():
                g = got.get(k, "<not observed>")
                ok = (g <= v) if k.startswith("max_") and isinstance(g, int) else g == v
                rows.append({"key": f"per_round.{k}", "expected": v, "observed": g, "ok": ok,
                             "rule": "le" if k.startswith("max_") else "eq"})
        elif key.endswith("_min"):
            ok = isinstance(got, int | float) and got >= want
            rows.append({"key": key, "expected": want, "observed": got, "ok": ok, "rule": "ge"})
        elif key == "cells_as_expected_percent":
            ok = float(got) == float(want)
            rows.append({"key": key, "expected": want, "observed": got, "ok": ok, "rule": "eq"})
        else:
            rows.append({"key": key, "expected": want, "observed": got, "ok": got == want, "rule": "eq"})
    return rows


def norm(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------- audit evidence (E-01, E-04)


class AuditCollector:
    """Before every reset (and at the end of each scenario) export the audit log through the API, verify it with the
    standalone verifier CLI and compare the application's account of handoffs with the independent ledgers."""

    def __init__(self, stack: Stack, api: Api, out: Path) -> None:
        self.stack, self.api, self.out = stack, api, out / "audit"
        self.out.mkdir(parents=True, exist_ok=True)
        self.scenario = "setup"
        self.segment = 0
        self.verify: list[dict[str, Any]] = []
        self.compare: list[dict[str, Any]] = []

    def collect(self) -> None:
        if self.stack.count("audit_events") == 0:
            return
        self.segment += 1
        name = f"{self.scenario}-{self.segment:02d}"
        r = self.api("helena").post("/api/v1/audit/exports")
        r.raise_for_status()
        body = r.json()
        jsonl, manifest = self.out / f"{name}.jsonl", self.out / f"{name}.manifest.json"
        jsonl.write_text(body["jsonl"], encoding="utf-8")
        manifest.write_text(json.dumps(body["manifest"], indent=1, sort_keys=True) + "\n", encoding="utf-8")
        events = [json.loads(line) for line in body["jsonl"].splitlines() if line.strip()]
        ops = sorted({e["operation_id"] for e in events if e.get("operation_id")})
        cmd = [sys.executable, str(REPO / "oracles/audit_verify.py"), str(jsonl), str(manifest)]
        if self.scenario in RECONSTRUCT and ops:
            cmd += ["--reconstruct", *ops]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 (fixed argv)
        doc = json.loads(res.stdout) if res.stdout.strip() else {"error": res.stderr[-500:]}
        self.verify.append({"segment": name, "exit": res.returncode,
                            "verdict": doc.get("verification", {}).get("verdict"),
                            "count": doc.get("verification", {}).get("count"),
                            "reconstructions": [{"rules_pass": x["rules_pass"], "transmissions": x["transmissions"],
                                                 "problems": x["problems"]} for x in doc.get("reconstructions", [])],
                            "command": " ".join(["python3", "oracles/audit_verify.py", f"audit/{name}.jsonl",
                                                 f"audit/{name}.manifest.json"]
                                                + (["--reconstruct", "<ops>"] if "--reconstruct" in cmd else []))})
        self._compare(name, events)

    def _compare(self, name: str, events: list[dict[str, Any]]) -> None:
        started: dict[str, dict[str, Any]] = {}
        for e in events:
            cv = (e.get("correlation") or {}).get("correlation_value")
            if e["event_type"] == "TRANSMISSION_STARTED" and cv:
                rec = started.setdefault(cv, {"started": 0, "interrupted": 0, "backend": None})
                rec["started"] += 1
            if e["event_type"] == "TRANSMISSION_INTERRUPTED" and cv:
                started.setdefault(cv, {"started": 0, "interrupted": 0, "backend": None})["interrupted"] += 1
        backends = dict(self.stack.sql("SELECT correlation_value, backend_id FROM lbf.operations"))
        wire = self.stack.tap.e8_counts()
        received = self.stack.skarvik.received()
        for cv, rec in sorted(started.items()):
            backend = backends.get(cv)
            if backend == "halden":
                oracle = wire.get(cv, 0)
                source = "wire-tap E8 ledger"
            else:
                claims = sum(1 for r in received if str(r.get("msgid") or r.get("message_id") or "").startswith(cv))
                staged = sum(1 for d in ("inbox", "withdrawn") for _ in (EXCHANGE / d).glob(f"ORD_{cv}-*.csv"))
                oracle = claims + staged
                source = "SKARVIK received ledger + inbox/withdrawn listing"
            observed = rec["started"] - rec["interrupted"]
            # interrupted = the app says "may or may not have reached": then wire ∈ [observed, started]
            equal = oracle == rec["started"] if rec["interrupted"] == 0 else observed <= oracle <= rec["started"]
            self.compare.append({"segment": name, "backend": backend, "app_transmissions_started": rec["started"],
                                 "app_interrupted": rec["interrupted"], "oracle_handoffs": oracle,
                                 "oracle": source, "consistent": equal})


# ----------------------------------------------------------------------------- runner


def run_inprocess(scenario: str, workdir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    obs_dir = workdir / scenario
    obs_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY",)}
    env.update({"LBF_RES_OBS_DIR": str(obs_dir), "LBF_LIVE_INFERENCE": "false"})
    cmd = ["uv", "run", "--frozen", "pytest", "-q", "-p", "no:cacheprovider", *INPROCESS[scenario]]
    t0 = time.monotonic()
    res = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True,  # noqa: S603 (fixed argv)
                         text=True, check=False)
    tail = res.stdout.strip().splitlines()[-3:]
    obs = {p.stem.split("__", 1)[1]: json.loads(p.read_text()) for p in sorted(obs_dir.glob(f"{scenario}__*.json"))}
    return obs, {"pytest_exit": res.returncode, "pytest_tail": tail, "seconds": round(time.monotonic() - t0, 1),
                 "command": "cd backend && " + " ".join(cmd)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all", help="R01..R18, comma-separated, or all")
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    random.seed(args.seed)
    wanted = ORDER if args.scenario == "all" else [s.strip().upper() for s in args.scenario.split(",")]
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    stack = Stack()
    stack.restart_app()  # (re)create the app with the test-profile environment and the pause-point bind mount
    stack.wait_ready()
    api = Api()
    for name in ("daniel", "priya", "marta", "rafael", "helena"):
        api(name).ensure_session()
    collector = AuditCollector(stack, api, out)
    stack.reset()  # initial conditions before any evidence is collected: never compare state from before the run
    stack.on_reset = collector.collect
    ctx = Ctx(stack, api)
    work = Path(tempfile.mkdtemp(prefix="lbf-res-"))
    tsv = ["scenario\tvariant\tkey\trule\texpected\tobserved\tok"]
    summary: dict[str, Any] = {"seed": args.seed, "scenarios": {}, "started_at": "<ts>"}
    all_ok = True
    for sid in wanted:
        collector.scenario, collector.segment = sid, 0
        spec = yaml.safe_load((EXPECTED / f"{sid}.yaml").read_text())
        expected = {v["name"]: v.get("expected", {}) for v in spec["variants"]}
        observed: dict[str, dict[str, Any]] = {}
        meta: dict[str, Any] = {}
        t0 = time.monotonic()
        if sid in LIVE:
            try:
                for variant, obs in LIVE[sid](ctx):
                    observed[variant] = obs
            except Exception as exc:  # recorded as a scenario failure with its cause
                meta["live_error"] = f"{type(exc).__name__}: {exc}"
            collector.collect()
        if sid in INPROCESS:
            obs, meta["inprocess"] = run_inprocess(sid, work)
            observed.update(obs)
        meta["seconds"] = round(time.monotonic() - t0, 1)
        variants = {}
        for vname, want in expected.items():
            got = observed.get(vname)
            rows = compare(want, got) if got is not None else [
                {"key": "*", "expected": "variant executed", "observed": "<not executed>", "ok": False, "rule": "eq"}]
            ok = all(r["ok"] for r in rows)
            variants[vname] = {"verdict": "PASS" if ok else "FAIL", "comparison": rows}
            for r in rows:
                tsv.append("\t".join([sid, vname, r["key"], r["rule"], norm(r["expected"]), norm(r["observed"]),
                                      "1" if r["ok"] else "0"]))
            tsv.append("\t".join([sid, vname, "VERDICT", "-", "PASS", variants[vname]["verdict"],
                                  "1" if ok else "0"]))
        scenario_ok = all(v["verdict"] == "PASS" for v in variants.values()) and "live_error" not in meta and \
            meta.get("inprocess", {}).get("pytest_exit", 0) == 0
        resends = [(vn, o.get("resends")) for vn, o in observed.items() if (o.get("resends") or 0) > 0]
        doc = {"id": sid, "title": spec.get("title"), "seed": args.seed, "inputs": spec, "observations": observed,
               "variants": variants, "meta": meta, "s7_1_more_than_one_handoff": resends,
               "verdict": "PASS" if scenario_ok else "FAIL"}
        (out / f"{sid}.json").write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
        summary["scenarios"][sid] = {"verdict": doc["verdict"], "seconds": meta["seconds"],
                                     "variants": {k: v["verdict"] for k, v in variants.items()}}
        all_ok = all_ok and scenario_ok
        print(f"{sid} {doc['verdict']} ({meta['seconds']} s) " + " ".join(f"{k}={v['verdict']}"
                                                                      for k, v in variants.items()), flush=True)
    stack.on_reset = None
    (out / "audit" / "verify.txt").write_text(json.dumps(collector.verify, indent=1, sort_keys=True) + "\n")
    (out / "audit" / "compare.json").write_text(json.dumps(collector.compare, indent=1, sort_keys=True) + "\n")
    e01 = bool(collector.verify) and all(v["exit"] == 0 and v["verdict"] == "VALID" for v in collector.verify)
    e04 = bool(collector.compare) and all(c["consistent"] for c in collector.compare)
    (out / "outcomes.tsv").write_text("\n".join(tsv) + "\n", encoding="utf-8")
    summary.update({"passed": sum(1 for s in summary["scenarios"].values() if s["verdict"] == "PASS"),
                    "total": len(wanted), "e01_all_exports_valid": e01, "e04_app_account_equals_oracles": e04,
                    "e04_rows": len(collector.compare), "exports": len(collector.verify),
                    "outcomes_tsv_sha256": hashlib.sha256((out / "outcomes.tsv").read_bytes()).hexdigest()})
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in ("passed", "total", "e01_all_exports_valid",
                                              "e04_app_account_equals_oracles", "outcomes_tsv_sha256")}))
    return 0 if all_ok and e01 and e04 else 1


if __name__ == "__main__":
    sys.exit(main())
