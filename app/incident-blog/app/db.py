"""Incident store: its own PostgreSQL database ("incidents") on the project's RDS instance, separate from the
product data, so a problem in the product database never prevents recording the incident.
Sign-in: IAM database authentication with the pod's role (EKS Pod Identity), TLS verified, no password."""

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import telemetry



class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(140))
    severity: Mapped[str] = mapped_column(String(4), index=True)
    services: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(24), index=True)
    commander: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(200), default="manual", index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)


class Post(Base):
    """A timeline entry: a state change with its note, or a plain update."""
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    author: Mapped[str] = mapped_column(String(120))
    state_from: Mapped[str | None] = mapped_column(String(24), nullable=True)
    state_to: Mapped[str | None] = mapped_column(String(24), nullable=True)
    body: Mapped[str] = mapped_column(Text)


def iam_token_provider(rds, host: str, port: int, user: str, region: str):
    """do_connect hook: a fresh IAM authentication token is the password of every new connection."""
    def provide_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)
    return provide_token


def _postgres_engine():
    import boto3
    from sqlalchemy.engine import URL

    host, port, user = os.environ["DB_HOST"], int(os.environ.get("DB_PORT", "5432")), os.environ["DB_USER"]
    region = os.environ.get("AWS_REGION", "eu-west-2")
    url = URL.create("postgresql+psycopg", username=user, host=host, port=port, database=os.environ.get("DB_NAME", "incidents"),
                     query={"sslmode": os.environ.get("DB_SSLMODE", "verify-full"),
                            "sslrootcert": os.environ.get("DB_SSLROOTCERT", "/etc/ssl/rds/global-bundle.pem"),
                            "connect_timeout": "10"})
    # A token is checked when a connection OPENS (valid 15 minutes): recycle connections well before that
    engine = create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=600)
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
    telemetry.trace_engine(engine)   # SQL spans when OpenTelemetry is on
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
