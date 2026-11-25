#!/usr/bin/env python3
"""Link check (V9-2): every relative Markdown link and image in the given files/directories must resolve to an
existing file or directory in the repository. External (http/https/mailto) links are listed, not fetched (no network).

    python3 tools/check_links.py README.md docs
Exit 0: no broken link.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def files(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        out += sorted(p.rglob("*.md")) if p.is_dir() else [p]
    return out


def main(argv: list[str]) -> int:
    broken, checked, external = [], 0, 0
    for f in files(argv):
        text = f.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```", "", text, flags=re.S)  # code blocks are not links
        for target in LINK.findall(text):
            if re.match(r"^(https?:|mailto:)", target):
                external += 1
                continue
            if target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            checked += 1
            if not (f.parent / path).exists():
                broken.append(f"{f}: {target}")
    for b in broken:
        print("BROKEN", b)
    print(f"links checked: {checked}; external (not fetched): {external}; broken: {len(broken)}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["README.md", "docs"]))
