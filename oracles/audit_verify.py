#!/usr/bin/env python3
"""Standalone audit verifier entry point (standard library only; never imports ``lbf``).

Usage:
  python3 oracles/audit_verify.py EXPORT.jsonl MANIFEST.json [--reconstruct OPERATION_ID ...]
Exit 0: VALID (and all reconstruction rules pass); 1: INVALID or a rule fails; 2: usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from lbf_oracles.audit_verify import parse_jsonl, reconstruct, verify_export  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an LBF audit export against its manifest.")
    parser.add_argument("export")
    parser.add_argument("manifest")
    parser.add_argument("--reconstruct", nargs="*", default=[])
    args = parser.parse_args(argv)
    try:
        data = Path(args.export).read_bytes()
        manifest = json.loads(Path(args.manifest).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"verdict": "ERROR", "reason": str(exc)}))
        return 2
    verdict = verify_export(data, manifest)
    out = {"verification": verdict.as_dict()}
    ok = verdict.valid
    if verdict.valid and args.reconstruct:
        events = parse_jsonl(data)
        out["reconstructions"] = [reconstruct(events, op) for op in args.reconstruct]
        ok = ok and all(r["rules_pass"] for r in out["reconstructions"])
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
