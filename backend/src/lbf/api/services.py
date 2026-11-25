"""Process-wide services: database engines, auth, the workflow context (backends) and the engine runner.

One application process owns the single HALDEN session (ADR-F04). Built from the environment in production;
tests build it directly with fakes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from lbf.auth.service import AuthService
from lbf.workflow.context import WorkflowContext


@dataclass
class AppConfig:
    profile: str = "default"  # default | demo | test
    public_origin: str = "http://127.0.0.1:8080"
    cookie_secure: bool = False
    engines_enabled: bool = True
    static_dir: Path | None = None
    halden_url: str = "http://halden:8090"
    skarvik_control_url: str = "http://skarvik:8095"
    assistant_provider: str = "disabled"

    @classmethod
    def from_env(cls) -> AppConfig:
        static = os.environ.get("LBF_STATIC_DIR")
        return cls(
            profile=os.environ.get("LBF_PROFILE", "default"),
            public_origin=os.environ.get("LBF_PUBLIC_ORIGIN", "http://127.0.0.1:8080"),
            cookie_secure=os.environ.get("LBF_COOKIE_SECURE", "false").lower() == "true",
            engines_enabled=os.environ.get("LBF_ENGINES_ENABLED", "true").lower() == "true",
            static_dir=Path(static) if static else None,
            halden_url=os.environ.get("LBF_HALDEN_URL", "http://halden:8090"),
            skarvik_control_url=os.environ.get("LBF_SKARVIK_CONTROL_URL", "http://skarvik:8095"),
            assistant_provider=os.environ.get("LBF_ASSISTANT_PROVIDER", "disabled"),
        )

    def redacted(self) -> dict[str, Any]:
        return {"profile": self.profile, "public_origin": self.public_origin, "cookie_secure": self.cookie_secure,
                "engines_enabled": self.engines_enabled, "halden_url": self.halden_url,
                "assistant_provider": self.assistant_provider, "secrets": "*** (Docker secret files; never shown)"}


@dataclass
class AppServices:
    config: AppConfig
    engine: Engine
    audit_engine: Engine | None
    auth: AuthService
    workflow: WorkflowContext
    runner: Any = None  # lbf.engines.runner.EngineRunner when engines run in this process
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def clock(self) -> Any:
        return self.workflow.clock
