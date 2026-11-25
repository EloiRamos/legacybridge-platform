#!/usr/bin/env python3
"""Verify the frozen v1 imports byte-for-byte.

Checks, for every entry of the import manifests:
  1. the file in this repository hashes to the manifest value;
  2. (cross-check) the manifest value equals the SHA-256 of the git blob at the
     pinned v1 revision (r2 ``0a896d1``), resolved with ``git ls-files -s``.

Also checks that ``v1bridge/__init__.py`` (the only added file) is empty and that
the SKARVIK freeze manifest (if present) verifies.

Standard library only. Exit 0 = all OK; 1 = mismatch; 2 = usage/environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V1_REVISION = "0a896d10c0d702ea295f7666d632237c80781b2a"
# A clone of the published predecessor, for the optional cross-check of the manifests against its git blobs.
DEFAULT_V1_REPOS = (
    ROOT / "governance" / "baseline" / "v1-clone",
    *([Path(os.environ["LBF_V1_REPO"])] if os.environ.get("LBF_V1_REPO") else []),
)

# local path prefix -> v1 path prefix
PATH_MAP = (
    ("legacy-systems/halden-erp/", "legacy-erp/"),
    ("backend/src/lbf/adapters/halden/v1bridge/", "src/legacybridge/bridge/"),
    ("docs/contracts/HALDEN_LEGACY_API.md", "docs/LEGACY_API.md"),
)
MANIFESTS = (
    "legacy-systems/HALDEN_IMPORT.sha256",
    "backend/src/lbf/adapters/halden/V1BRIDGE_IMPORT.sha256",
)
SKARVIK_MANIFEST = "legacy-systems/skarvik-oes/SKARVIK_MANIFEST.sha256"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(path: Path) -> list[tuple[str, str]]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        entries.append((digest.strip(), rel.strip()))
    return entries


def v1_path(rel: str) -> str:
    for ours, theirs in PATH_MAP:
        if rel.startswith(ours):
            return theirs + rel[len(ours):] if ours.endswith("/") else theirs
    raise KeyError(rel)


def blob_sha256(repo: Path, path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", V1_REVISION, "--", path],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        if len(out) < 3:
            return None
        blob = out[2]
        data = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", blob], check=True, capture_output=True
        ).stdout
        return sha256_bytes(data)
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v1-repo", type=Path, default=None)
    args = parser.parse_args()

    repo = args.v1_repo
    if repo is None:
        repo = next((r for r in DEFAULT_V1_REPOS if (r / ".git").exists()), None)

    failures = 0
    checked = 0
    cross_checked = 0
    for manifest in MANIFESTS:
        mpath = ROOT / manifest
        if not mpath.exists():
            print(f"MISSING MANIFEST {manifest}")
            return 2
        for digest, rel in read_manifest(mpath):
            checked += 1
            local = ROOT / rel
            if not local.exists():
                print(f"FAILED (missing) {rel}")
                failures += 1
                continue
            actual = sha256_bytes(local.read_bytes())
            if actual != digest:
                print(f"FAILED (local hash) {rel}")
                failures += 1
                continue
            if repo is not None:
                upstream = blob_sha256(repo, v1_path(rel))
                if upstream != digest:
                    print(f"FAILED (v1 blob {V1_REVISION[:7]}) {rel}")
                    failures += 1
                    continue
                cross_checked += 1
            print(f"OK {rel}")

    init = ROOT / "backend/src/lbf/adapters/halden/v1bridge/__init__.py"
    if not init.exists() or init.read_bytes() != b"":
        print("FAILED v1bridge/__init__.py must exist and be empty")
        failures += 1

    skarvik = ROOT / SKARVIK_MANIFEST
    if skarvik.exists():
        base = skarvik.parent
        for digest, rel in read_manifest(skarvik):
            checked += 1
            f = base / rel
            if not f.exists() or sha256_bytes(f.read_bytes()) != digest:
                print(f"FAILED (skarvik freeze) {rel}")
                failures += 1

    if repo is None:
        print("NOTE v1 blob cross-check not executed (no v1 repository available)")
    print(
        f"SUMMARY checked={checked} cross_checked={cross_checked} failures={failures} "
        f"v1_repo={'none' if repo is None else repo}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
