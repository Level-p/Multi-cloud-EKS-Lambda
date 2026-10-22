"""Mobile data in PostgreSQL: database "mobile" on the project's RDS instance (next to "agripulse" and
"incidents"). SQLite in the tests.

The Lambda signs in with IAM database authentication: its execution role may rds-db:connect as
agripulse_mobile only; boto3 signs a 15-minute token locally (no network call) and the token is the
password of each new connection, so no database password exists anywhere. TLS is verified against the RDS
CA bundle packaged with the code. All queries use bound parameters (no SQL built from strings).
"""

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class FieldRow(Base):
    __tablename__ = "fields"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)   # Cognito "sub" of the farmer
    name: Mapped[str] = mapped_column(String(80))
    crop: Mapped[str] = mapped_column(String(40))
    area_ha: Mapped[float] = mapped_column(Float)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    field_id: Mapped[str] = mapped_column(ForeignKey("fields.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    growth_stage: Mapped[str] = mapped_column(String(20))
    issue: Mapped[str] = mapped_column(String(20), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    media_keys: Mapped[str] = mapped_column(Text, default="[]")   # JSON list of S3 keys under uploads/<sub>/


def iam_token_provider(rds, host: str, port: int, user: str, region: str):
    """do_connect hook: a fresh IAM authentication token is the password of every new connection."""
    def provide_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)
    return provide_token


def _postgres_engine():
    """One connection per Lambda instance: the instance serves one request at a time, and the account's
    concurrency limit then bounds the connections the mobile API can open on the db.t3.micro."""
    import boto3
    from sqlalchemy.engine import URL

    host, port, user = os.environ["DB_HOST"], int(os.environ.get("DB_PORT", "5432")), os.environ["DB_USER"]
    region = os.environ.get("AWS_REGION", "eu-west-2")
    url = URL.create("postgresql+psycopg", username=user, host=host, port=port, database=os.environ.get("DB_NAME", "mobile"),
                     query={"sslmode": os.environ.get("DB_SSLMODE", "verify-full"),
                            "sslrootcert": os.environ.get("DB_SSLROOTCERT", "global-bundle.pem"), "connect_timeout": "5"})
    engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0, pool_recycle=600)
    event.listen(engine, "do_connect", iam_token_provider(boto3.client("rds", region_name=region), host, port, user, region))
    return engine


def make_session_factory(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL")
    if url and url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    elif url:
        engine = create_engine(url, pool_pre_ping=True)
    else:
        engine = _postgres_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
