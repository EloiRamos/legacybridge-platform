"""Alembic environment: runs as lbf_migrator. DSN from lbf.persistence.settings."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, text

from lbf.persistence.settings import DbSettings

config = context.config


def run_migrations_online() -> None:
    url = config.attributes.get("url") or DbSettings.from_env().url("migrator")
    engine = create_engine(url, future=True)
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS lbf"))
        connection.commit()
        context.configure(connection=connection, version_table_schema="lbf", transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
