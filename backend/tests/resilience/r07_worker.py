"""R07 variant B worker: a separate OS process running its own instance of the API application against the shared
test database (its own connection pool, its own engine objects; nothing shared with the parent but PostgreSQL)."""

from __future__ import annotations

from typing import Any


def serve(passwords: dict[str, str], db_name: str, port: int, tmp: str, persona: str, inbox: Any, outbox: Any,
          barrier: Any) -> None:
    from pathlib import Path

    import workflow_support
    from api_support import Api
    from sqlalchemy import select

    from lbf.persistence import tables as t
    from lbf.persistence.db import make_engine
    from lbf.persistence.settings import DbSettings

    settings = DbSettings(host="127.0.0.1", port=port, name=db_name, passwords=passwords)
    engine = make_engine(settings, "app", pool_size=4)

    def existing_user(eng: Any, username: str, *roles: Any, **_: Any) -> Any:
        with eng.connect() as conn:
            return conn.execute(select(t.users.c.id).where(t.users.c.username == username)).scalar_one()

    workflow_support.make_user = existing_user  # users were created by the parent process
    api = Api(workflow_support.WF(engine, Path(tmp)))
    client = api.as_(persona)
    outbox.put(("ready", persona))
    while True:
        item = inbox.get()
        if item is None:
            break
        op, ref = item
        barrier.wait()
        r = client.post(f"/api/v1/operations/{op}/approve", json=ref)
        outbox.put((persona, r.status_code, r.json().get("code") if r.status_code >= 400 else r.json().get("state")))
    engine.dispose()
