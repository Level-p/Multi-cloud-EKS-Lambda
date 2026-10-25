"""The shop database (shop_<environment>): orders. One order per farm, so each farmer handles their own part.
Item rows keep a copy of the title and price at the time of the order: later price changes never alter an order."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[str] = mapped_column(String(36), index=True)
    buyer_name: Mapped[str] = mapped_column(String(100))
    buyer_email: Mapped[str] = mapped_column(String(254))
    farmer_id: Mapped[str] = mapped_column(String(36), index=True)
    farm_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(12), index=True)   # placed accepted dispatched delivered cancelled
    delivery_address: Mapped[str] = mapped_column(String(300))
    phone: Mapped[str] = mapped_column(String(30))
    total: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    currency: Mapped[str] = mapped_column(String(3))
    payment: Mapped[str] = mapped_column(String(20), default="on-delivery")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", lazy="selectin", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    listing_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(12))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2, asdecimal=False))
    quantity: Mapped[float] = mapped_column(Float)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2, asdecimal=False))
    order: Mapped[Order] = relationship(back_populates="items")
