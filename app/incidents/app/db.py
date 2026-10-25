"""The incident store: its own database (incidents_<environment>), separate from the product data, so a problem in
the product databases never prevents recording the incident."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
