"""Engine factory (psycopg 3). One engine per database role."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from lbf.persistence.settings import DbSettings


def make_engine(settings: DbSettings, role: str = "app", *, database: str | None = None, pool_size: int = 10) -> Engine:
    return create_engine(
        settings.url(role, database=database),
        pool_size=pool_size,
        max_overflow=10,
        pool_pre_ping=True,
        connect_args={"options": "-c search_path=lbf -c timezone=UTC"},
    )
