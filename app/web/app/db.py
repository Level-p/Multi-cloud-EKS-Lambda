"""PostgreSQL access (SQLAlchemy 2): database "agripulse" on the project's RDS instance. SQLite in the tests.

In EKS the pod signs in with IAM database authentication: EKS Pod Identity gives it AWS credentials for its
role (rds-db:connect on its own database user only), boto3 signs a 15-minute token with them, and the token
is the password of each new connection, so no database password exists anywhere. TLS is verified against
the RDS CA bundle in the image. All queries use bound parameters (no SQL built from strings).
"""

import os
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import telemetry


class Base(DeclarativeBase):
    pass


class Farm(Base):
    __tablename__ = "farms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(60), index=True)
    hectares: Mapped[float] = mapped_column(Float)


class Harvest(Base):
    __tablename__ = "harvests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40))
    season: Mapped[str] = mapped_column(String(20))
    yield_tonnes: Mapped[float] = mapped_column(Float)
    recorded_by: Mapped[str] = mapped_column(String(120))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MarketPrice(Base):
    __tablename__ = "market_prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crop: Mapped[str] = mapped_column(String(40), index=True)
    market: Mapped[str] = mapped_column(String(60))
    price_per_tonne: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3))
    on: Mapped[date] = mapped_column(Date, index=True)


def iam_token_provider(rds, host: str, port: int, user: str, region: str):
    """do_connect hook: a fresh IAM authentication token is the password of every new connection."""
    def provide_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)
    return provide_token


def postgres_url():
    from sqlalchemy.engine import URL

    return URL.create("postgresql+psycopg", username=os.environ["DB_USER"], host=os.environ["DB_HOST"],
                      port=int(os.environ.get("DB_PORT", "5432")), database=os.environ["DB_NAME"],
                      query={"sslmode": os.environ.get("DB_SSLMODE", "verify-full"),
                             "sslrootcert": os.environ.get("DB_SSLROOTCERT", "/etc/ssl/rds/global-bundle.pem"),
                             "connect_timeout": "10"})


def _postgres_engine():
    import boto3

    url, region = postgres_url(), os.environ.get("AWS_REGION", "eu-west-2")
    # A token is checked when a connection OPENS (valid 15 minutes): recycle connections well before that
    engine = create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=600)
    event.listen(engine, "do_connect", iam_token_provider(boto3.client("rds", region_name=region), url.host, url.port,
                                                          url.username, region))
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
