#!/usr/bin/env python3
"""Secret scan (V8-6 ``make secret-scan``; SEC-10 over the captured outputs of an acceptance run). Stdlib only.

Searches for (a) every value in ``deploy/secrets/*.txt`` except the OpenAI key, which is
never opened, and every demo password (≥ 8 characters), and (b) credential
patterns: OpenAI-style keys, private-key headers, cloud/GitHub tokens, bearer tokens with a value. Never prints a
secret: findings name the file, line, rule and an 8-character SHA-256 prefix of the match.

    python3 acceptance/secret_scan.py --repo                 # git-tracked + untracked-not-ignored files
    python3 acceptance/secret_scan.py --paths DIR [DIR ...]  # captured outputs (logs, evidence, reports)

Exit 0: no findings; 1: findings; 2: usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRETS = REPO / "deploy" / "secrets"
PATTERNS = {
    "openai-key": re.compile(rb"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    # an OPENAI_API_KEY assignment carrying a literal value, whatever the value looks like (not only sk-).
    # Forms: KEY=v, KEY = v, KEY: v, "KEY": "v", export KEY='v'. Not matched: OPENAI_API_KEY_FILE, empty values,
    # references (${...}, $VAR, <...>), values under 8 characters (doc examples such as sk-...) and placeholders.
    "openai-key-assignment": re.compile(
        rb"\bOPENAI_API_KEY[\"']?[ \t]*[:=][ \t]*[\"']?"
        rb"(?!(?i:your|placeholder|changeme|change-me|example|dummy|redacted|replace|test-key-not-a-secret|xxxx))"
        rb"[A-Za-z0-9_.-]{8,}"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "bearer-value": re.compile(rb"(?i)authorization:\s*bearer\s+(?!test-key-not-a-secret)[A-Za-z0-9._-]{16,}"),
}
# Public, documented fixture values of the frozen simulators (they are part of the simulators' published contracts,
# not deployment secrets) are not secrets; anything else matching a deploy/secrets value is a finding.
PUBLIC_FIXTURES = {b"halden-demo-secret"}
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def secret_values() -> list[tuple[str, bytes]]:
    values: list[tuple[str, bytes]] = []
    if not SECRETS.is_dir():
        return values
    for f in sorted(SECRETS.glob("*.txt")):
        if f.name == "openai_api_key.txt":
            continue  # only the application reads the key; the patterns below cover its form
        if f.name == "demo-credentials.txt":
            for line in f.read_text("utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and len(parts[1]) >= 8:
                    values.append((f"demo password ({parts[0]})", parts[1].encode()))
            continue
        v = f.read_text("utf-8").strip().encode()
        if len(v) >= 8 and v not in PUBLIC_FIXTURES:
            values.append((f"deploy/secrets/{f.name}", v))
    return values


def repo_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,  # noqa: S607
                         capture_output=True, check=True).stdout
    return [REPO / p.decode() for p in out.split(b"\0") if p]


def walk(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file() and not SKIP_DIRS & set(f.parts)]
    return files


def scan(files: list[Path]) -> tuple[list[dict[str, object]], int]:
    values = secret_values()
    findings: list[dict[str, object]] = []
    scanned = 0
    for f in files:
        if not f.is_file() or f.is_relative_to(SECRETS):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        scanned += 1
        rel = str(f.relative_to(REPO)) if f.is_relative_to(REPO) else str(f)
        for label, v in values:
            for m in re.finditer(re.escape(v), data):
                findings.append({"file": rel, "line": data.count(b"\n", 0, m.start()) + 1, "rule": f"value:{label}",
                                 "match_sha256_prefix": hashlib.sha256(v).hexdigest()[:8]})
        for rule, rx in PATTERNS.items():
            for m in rx.finditer(data):
                findings.append({"file": rel, "line": data.count(b"\n", 0, m.start()) + 1, "rule": rule,
                                 "match_sha256_prefix": hashlib.sha256(m.group(0)).hexdigest()[:8]})
    return findings, scanned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo", action="store_true")
    g.add_argument("--paths", nargs="+")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    files = repo_files() if args.repo else walk(args.paths)
    findings, scanned = scan(files)
    report = {"scope": "repo" if args.repo else args.paths, "files_scanned": scanned,
              "secret_values_checked": len(secret_values()), "patterns": sorted(PATTERNS),
              "public_fixture_values_excluded": len(PUBLIC_FIXTURES), "findings": findings}
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text if findings else json.dumps({k: v for k, v in report.items() if k != "findings"} | {"findings": 0}))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
