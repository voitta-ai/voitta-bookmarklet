"""Command line for the users store (server mode). Run inside the backend env/container:

    python -m app.users seed --password 314159 --admin a@x.com b@y.com c@z.com
    python -m app.users list
    python -m app.users set-password someone@x.com 'new password'
    python -m app.users enable someone@x.com

``seed`` is idempotent: it invites missing emails (as users, ``--admin`` ones as admins) and gives
the password to anyone who has none yet; it never overwrites an existing password or role.
Owners come from ``VOITTA_OWNERS``, not from here.
"""

from __future__ import annotations

import argparse
import sys
import time

from app.services import passwords, users


def _seed(args) -> int:
    admins = {e for e in (users.normalize_email(a) for a in args.admin) if e}
    emails = [*args.emails, *args.admin]
    if args.password and passwords.problem(args.password):
        print(passwords.problem(args.password), file=sys.stderr)
        return 2
    for raw in emails:
        e = users.normalize_email(raw)
        if e is None:
            print(f"invalid   {raw}")
            continue
        role = "admin" if e in admins else "user"
        status = users.invite([e], role=role, password=args.password, actor="seed")[0]["status"]
        u = users.get(e)
        if status == "exists" and args.password and u and not u.get("password_hash"):
            users.set_password(e, args.password, actor="seed")
            status = "password set"
        print(f"{status:<13} {e}  ({users.role_of(users.get(e))})")
    return 0


def _list(_args) -> int:
    for u in users.list_users():
        flags = ["password" if u["has_password"] else "", "google" if u["has_google"] else "",
                 "DISABLED" if u["disabled"] else "", f"locked {u['locked_minutes']}m" if u["locked_minutes"] else ""]
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["last_seen"])) if u["last_seen"] else "never"
        print(f"{u['email']:<40} {u['role']:<6} {seen:<17} {' '.join(f for f in flags if f)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="invite emails (idempotent)")
    s.add_argument("--password", default=None)
    s.add_argument("--admin", action="append", default=[], help="email to invite as admin (repeatable)")
    s.add_argument("emails", nargs="*")
    sub.add_parser("list")
    sp = sub.add_parser("set-password")
    sp.add_argument("email")
    sp.add_argument("password")
    for name in ("enable", "disable", "unlock"):
        sub.add_parser(name).add_argument("email")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "seed":
            return _seed(a)
        if a.cmd == "list":
            return _list(a)
        if a.cmd == "set-password":
            users.set_password(a.email, a.password, actor="cli")
        else:
            getattr(users, a.cmd)(a.email, actor="cli")
        print("ok")
        return 0
    except users.UserError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
