"""Operator commands, run inside an accounts pod (never exposed over HTTP):

    POD=$(kubectl -n accounts-production get pod -l app.kubernetes.io/name=accounts -o name | head -1)
    kubectl -n accounts-production exec "$POD" -- python -m app.admin grant-ops someone@example.com
    kubectl -n accounts-production exec "$POD" -- python -m app.admin set-role someone@example.com buyer

grant-ops gives an existing account access to the incident blog. The person signs in again to get a token
with the new role.
"""

import sys

from sqlalchemy import select

from agripulse_common.auth import ROLES

from .main import sessions
from .db import User


def set_role(email: str, role: str) -> str:
    if role not in ROLES:
        raise SystemExit(f"role must be one of {', '.join(ROLES)}")
    with sessions()() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            raise SystemExit(f"no account for {email}: the person registers first")
        user.role = role
        db.commit()
        return f"{user.email} is now {role} (sign in again to use it)"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "grant-ops":
        print(set_role(argv[1], "ops"))
        return 0
    if len(argv) == 3 and argv[0] == "set-role":
        print(set_role(argv[1], argv[2]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
