"""Write the committed OpenAPI document (``make openapi`` → ``docs/api/openapi.json``). Deterministic output.

Generated with the ``demo`` profile so the profile-gated demo endpoints are documented (they are absent from
the running API in the ``default`` profile, SEC-15)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

from lbf.api.app import create_app
from lbf.api.services import AppConfig, AppServices


def document() -> dict[str, Any]:
    svc = AppServices(config=AppConfig(profile="demo", engines_enabled=False), engine=cast(Any, None),
                      audit_engine=None, auth=cast(Any, None), workflow=cast(Any, None))
    return create_app(svc).openapi()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    text = json.dumps(document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args:
        Path(args[0]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
