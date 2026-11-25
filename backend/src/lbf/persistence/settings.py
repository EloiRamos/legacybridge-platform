"""Database connection settings. Passwords come from Docker secret files (``*_FILE``) or env (local dev)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

ROLE_USERS = {"migrator": "lbf_migrator", "app": "lbf_app", "audit_reader": "lbf_audit_reader", "superuser": "postgres"}


def read_secret(name: str, default_file: str | None = None) -> str | None:
    path = os.environ.get(f"{name}_FILE") or default_file
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8").strip() or None
    value = os.environ.get(name)
    return value.strip() if value else None


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    passwords: dict[str, str | None] = field(repr=False)

    @classmethod
    def from_env(cls) -> DbSettings:
        return cls(
            host=os.environ.get("LBF_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("LBF_DB_PORT", "5432")),
            name=os.environ.get("LBF_DB_NAME", "lbf"),
            passwords={
                "migrator": read_secret("LBF_DB_MIGRATOR_PASSWORD"),
                "app": read_secret("LBF_DB_APP_PASSWORD"),
                "audit_reader": read_secret("LBF_DB_AUDIT_READER_PASSWORD"),
                "superuser": read_secret("LBF_DB_SUPERUSER_PASSWORD"),
            },
        )

    def url(self, role: str, *, database: str | None = None) -> str:
        user = ROLE_USERS[role]
        password = self.passwords.get(role)
        if not password:
            raise RuntimeError(f"no password configured for database role {user}")
        db = database or self.name
        return f"postgresql+psycopg://{user}:{quote(password, safe='')}@{self.host}:{self.port}/{db}"
