"""PostgreSQL (one database per service and environment on the project's RDS instance); SQLite in tests and scans.

In EKS the pod signs in with IAM database authentication: EKS Pod Identity gives it AWS credentials for its role
(rds-db:connect on its own database user only), boto3 signs a 15-minute token with them, and the token is the
password of each new connection, so no database password exists anywhere. TLS is verified against the RDS CA
bundle in the image. All queries use bound parameters (no SQL built from strings).

  DATABASE_URL   sqlite:///... (tests, DAST scan, local runs) or a full URL
  DB_HOST, DB_PORT, DB_NAME, DB_USER, AWS_REGION   otherwise: RDS with an IAM token
"""

import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL

from . import telemetry


def iam_token_provider(rds, host: str, port: int, user: str, region: str):
    """do_connect hook: a fresh IAM authentication token is the password of every new connection."""
    def provide_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)
    return provide_token


def postgres_url() -> URL:
    return URL.create("postgresql+psycopg", username=os.environ["DB_USER"], host=os.environ["DB_HOST"],
                      port=int(os.environ.get("DB_PORT", "5432")), database=os.environ["DB_NAME"],
                      query={"sslmode": os.environ.get("DB_SSLMODE", "verify-full"),
                             "sslrootcert": os.environ.get("DB_SSLROOTCERT", "/etc/ssl/rds/global-bundle.pem"),
                             "connect_timeout": "10"})


def make_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL")
    if url and url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    elif url:
        engine = create_engine(url, pool_pre_ping=True)
    else:
        import boto3

        pg, region = postgres_url(), os.environ.get("AWS_REGION", "eu-west-2")
        # A token is checked when a connection OPENS (valid 15 minutes): recycle connections well before that
        engine = create_engine(pg, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=600)
        event.listen(engine, "do_connect", iam_token_provider(boto3.client("rds", region_name=region), pg.host, pg.port,
                                                              pg.username, region))
    return telemetry.trace_engine(engine)   # SQL spans when OpenTelemetry is on


def make_session_factory(metadata, url: str | None = None):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(url)
    metadata.create_all(engine)   # each service owns its schema; tables are created on first start
    return sessionmaker(engine, expire_on_commit=False)


def ping(session_factory):
    with session_factory()() as db:
        db.execute(text("SELECT 1"))
