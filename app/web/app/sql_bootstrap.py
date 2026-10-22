"""Create the three databases and their IAM-authenticated users on the RDS PostgreSQL instance
(runs as an Argo CD PreSync Job in EKS before every sync of the web app; idempotent).

It is the only code that uses the master password: RDS keeps it in Secrets Manager, and only this Job's
ServiceAccount may read that secret (EKS Pod Identity). Every app then signs in with an IAM token instead:

  database    owner (= the only user that may connect)   used by
  agripulse   agripulse_web                              web app (EKS)
  incidents   agripulse_incidents                        incident blog (EKS)
  mobile      agripulse_mobile                           mobile API (Lambda)

Each user is a LOGIN role with rds_iam (no password) and owns its own database only: it can create its
tables there (PostgreSQL 15+: the public schema belongs to the database owner) and cannot connect to the
others (CONNECT is revoked from PUBLIC).

Env: DB_HOST, DB_PORT, MASTER_SECRET_ID, DB_USERS (JSON {"agripulse": "agripulse_web", ...}),
     AWS_REGION, DB_SSLROOTCERT
"""

import json
import os
import re
import sys

NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def quote_ident(name: str) -> str:
    """Identifiers cannot be bound parameters: accept only plain lower-case names, then quote them."""
    if not NAME.match(name):
        raise ValueError(f"unexpected database or user name: {name!r}")
    return f'"{name}"'


def plan(env=os.environ) -> dict[str, str]:
    """database -> the user that owns it"""
    databases = json.loads(env["DB_USERS"])
    for database, user in databases.items():
        quote_ident(database), quote_ident(user)
    return databases


def role_statements(user: str, master: str) -> list[str]:
    """Run after checking that the role does not exist yet (CREATE ROLE has no IF NOT EXISTS)."""
    u, m = quote_ident(user), quote_ident(master)
    return [f"CREATE ROLE {u} LOGIN", f"GRANT rds_iam TO {u}", f"GRANT {u} TO {m}"]


def database_statements(database: str, user: str, exists: bool) -> list[str]:
    d, u = quote_ident(database), quote_ident(user)
    first = f"ALTER DATABASE {d} OWNER TO {u}" if exists else f"CREATE DATABASE {d} OWNER {u}"
    return [first, f"REVOKE CONNECT, TEMPORARY ON DATABASE {d} FROM PUBLIC"]


def main():
    import boto3
    import psycopg

    region = os.environ.get("AWS_REGION", "eu-west-2")
    secret = json.loads(boto3.client("secretsmanager", region_name=region)
                        .get_secret_value(SecretId=os.environ["MASTER_SECRET_ID"])["SecretString"])
    master = secret["username"]
    with psycopg.connect(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")), dbname="postgres",
                         user=master, password=secret["password"], sslmode="verify-full",
                         sslrootcert=os.environ.get("DB_SSLROOTCERT", "/etc/ssl/rds/global-bundle.pem"),
                         autocommit=True) as conn:   # CREATE DATABASE cannot run inside a transaction
        for database, user in plan().items():
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone() is None:
                for stmt in role_statements(user, master):
                    conn.execute(stmt)
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone() is not None
            for stmt in database_statements(database, user, exists):
                conn.execute(stmt)
            print(f"{database}: owned by {user} (IAM authentication)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # make the Job fail visibly in Argo CD
        print(f"sql bootstrap failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
