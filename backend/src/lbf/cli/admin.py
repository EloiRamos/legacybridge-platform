"""``lbf-admin`` — user provisioning CLI.

Passwords are read from stdin, never from arguments. ``demo-users`` creates the seven fictional personas
with generated passwords and prints ``username<TAB>password<TAB>roles`` lines to stdout; the
``make demo-users`` target writes them to ``deploy/secrets/demo-credentials.txt`` with mode 0600.
"""

from __future__ import annotations

import argparse
import secrets
import sys

from sqlalchemy import select

from lbf.auth.service import AuthService
from lbf.domain.enums import Role
from lbf.domain.errors import DomainError
from lbf.persistence import tables as t
from lbf.persistence.db import make_engine
from lbf.persistence.settings import DbSettings

DEMO_USERS: list[tuple[str, str, list[Role]]] = [
    ("marta.ops", "Marta Kowalczyk (Operations Manager)", [Role.OPS_MANAGER]),
    ("daniel.sales", "Daniel Okoye (Sales Operations)", [Role.SALES_OPS]),
    ("priya.sales", "Priya Shah (Sales Operations)", [Role.SALES_OPS]),
    ("sanna.inventory", "Sanna Lindqvist (Inventory Coordinator)", [Role.INVENTORY_COORDINATOR]),
    ("rafael.integration", "Rafael Duarte (IT Integration Engineer)", [Role.INTEGRATION_ENGINEER]),
    ("helena.compliance", "Helena Brandt (Security & Compliance)", [Role.COMPLIANCE_REVIEWER]),
    ("tomas.admin", "Tomás Ibarra (System Administrator)", [Role.SYSADMIN]),
]


def _service() -> AuthService:
    return AuthService(make_engine(DbSettings.from_env(), "app", pool_size=1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lbf-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("--username", required=True)
    cu.add_argument("--display-name", required=True)
    cu.add_argument("--role", action="append", choices=[r.value for r in Role], default=[])
    for name in ("grant-role", "revoke-role"):
        p = sub.add_parser(name)
        p.add_argument("--username", required=True)
        p.add_argument("--role", required=True, choices=[r.value for r in Role])
    sub.add_parser("demo-users")
    args = parser.parse_args(argv)

    svc = _service()
    try:
        with svc.engine.begin() as conn:
            if args.cmd == "create-user":
                password = sys.stdin.readline().rstrip("\n")
                svc.create_user(
                    conn,
                    username=args.username,
                    display_name=args.display_name,
                    password=password,
                    roles=[Role(r) for r in args.role],
                    actor=None,
                )
                print(f"created {args.username}", file=sys.stderr)
            elif args.cmd in ("grant-role", "revoke-role"):
                row = conn.execute(select(t.users.c.id).where(t.users.c.username == args.username)).first()
                if row is None:
                    print("unknown user", file=sys.stderr)
                    return 2
                if args.cmd == "grant-role":
                    svc.grant_role(conn, row.id, Role(args.role), actor=None)
                else:
                    svc.revoke_role(conn, row.id, Role(args.role), actor=None)
            elif args.cmd == "demo-users":
                for username, display, roles in DEMO_USERS:
                    exists = conn.execute(select(t.users.c.id).where(t.users.c.username == username)).first()
                    if exists:
                        print(f"exists {username}", file=sys.stderr)
                        continue
                    password = secrets.token_urlsafe(18)
                    svc.create_user(
                        conn, username=username, display_name=display, password=password, roles=roles, actor=None
                    )
                    print(f"{username}\t{password}\t{','.join(r.value for r in roles)}")
    except DomainError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
