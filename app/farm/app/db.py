"""The farm database (farm_<environment>): farmers' profiles, what they sell, and their farm records.
Every row carries owner_id (the farmer's account id from the token): a farmer can only read or change their own."""

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Farm(Base):
    """One profile per farmer: who they are, where the farm is, how much it can produce."""
    __tablename__ = "farms"
    owner_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    farm_name: Mapped[str] = mapped_column(String(100))
    owner_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(30), default="")
    region: Mapped[str] = mapped_column(String(60), index=True)
    address: Mapped[str] = mapped_column(String(200), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    hectares: Mapped[float] = mapped_column(Float)
    capacity_tonnes_month: Mapped[float] = mapped_column(Float)   # what the farm can supply per month
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Listing(Base):
    """Something a farmer sells: produce (tomatoes, yam, eggs...) or a farm resource (seedlings, manure, tractor hire...)."""
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10), index=True)   # produce | resource
    category: Mapped[str] = mapped_column(String(40), index=True)
    unit: Mapped[str] = mapped_column(String(12))
    price: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False))
    currency: Mapped[str] = mapped_column(String(3))
    quantity: Mapped[float] = mapped_column(Float)   # in stock, in units; decreased atomically by orders
    description: Mapped[str] = mapped_column(Text, default="")
    image_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Activity(Base):
    """A farm record: planting, spraying, harvest, sale... the farmer's own log."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    on: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    crop: Mapped[str] = mapped_column(String(60), default="")
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(12), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
