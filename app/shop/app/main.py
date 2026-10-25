"""AgriPulse shop: buyers (and farmers buying farm resources) buy fresh produce and farm resources directly from farms.

Pages
  GET  /                      the catalog (kind, q)              GET /items/{id}    one item, add to cart
  POST /cart                  add to cart                        GET /cart          POST /cart/remove
  POST /checkout              delivery address + phone -> one order per farm, pay on delivery
  GET  /orders                my orders                          POST /orders/{id}/cancel   (while still placed)
  GET  /sales                 farmers: orders for my farm        POST /sales/{id}/status    accept/dispatch/deliver/decline
API (mobile app, Bearer token)
  GET  /api/catalog           GET /api/items/{id}               PUBLIC
  POST /api/orders            {items: [{listing_id, quantity}], delivery_address, phone}
  GET  /api/orders            POST /api/orders/{id}/cancel
  GET  /api/sales             POST /api/sales/{id}/status {status}

The catalog and items come from the farm service (it owns the listings) and are cached in Redis (60 s) in this
namespace; the cart lives in Redis too. An order reserves the stock at the farm service first (atomic there:
two buyers can never both get the last basket); if any item cannot be reserved, everything already reserved is
released and nothing is ordered. Cancelling an order gives the stock back.
"""

import logging
import os
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import select

from agripulse_common import auth, web
from agripulse_common.cache import Cache, make_client
from agripulse_common.db import make_session_factory, ping
from agripulse_common.services import ServiceClient, ServiceError

from .db import Base, Order, OrderItem

log = logging.getLogger("agripulse.shop")
ORDERS = Counter("agripulse_orders_total", "Orders by result", ["result"])
ORDER_VALUE = Counter("agripulse_order_value_total", "Value of orders placed", ["currency"])
STATUS_CHANGES = Counter("agripulse_order_status_changes_total", "Order status changes", ["to"])

BUYING_ROLES = ("buyer", "farmer")   # farmers buy farm resources too
# who may move an order from one status to another
FARMER_MOVES = {"placed": ("accepted", "cancelled"), "accepted": ("dispatched", "cancelled"), "dispatched": ("delivered",)}
BUYER_MOVES = {"placed": ("cancelled",)}
CART_TTL = 7 * 24 * 3600
TEXT = r"^[^<>]*$"
_sessions = None
_cache = None
_farm = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory(Base.metadata)
    return _sessions


def cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(make_client())
    return _cache


def farm() -> ServiceClient:
    global _farm
    if _farm is None:
        _farm = ServiceClient(os.environ.get("FARM_INTERNAL_URL", "http://farm:8080"))
    return _farm


app, templates = web.create_app("shop", Path(__file__).parent, readiness=lambda: ping(sessions),
                                loggers=("agripulse", "uvicorn.error"), redis=True)
buyer = auth.requires(*BUYING_ROLES)
buyer_page = auth.requires(*BUYING_ROLES, html=True)
farmer = auth.requires("farmer")
farmer_page = auth.requires("farmer", html=True)


# ---------------- the farm service, cached ----------------
def catalog(kind: str | None = None, q: str | None = None) -> list[dict]:
    params = {k: v for k, v in {"kind": kind, "q": q, "limit": 96}.items() if v}

    def load():
        return farm().call("GET", "/api/catalog?" + urlencode(params))
    try:
        return cache().get_or_load("catalog:" + urlencode(sorted(params.items())), 60, load, "catalog")
    except ServiceError as err:
        log.warning("catalog unavailable: %s", err)
        raise HTTPException(status_code=503, detail="the catalog is unavailable, try again shortly") from None


def item(listing_id: int) -> dict:
    try:
        return cache().get_or_load(f"item:{listing_id}", 30, lambda: farm().call("GET", f"/api/listings/{listing_id}"), "item")
    except ServiceError as err:
        raise HTTPException(status_code=404 if err.status == 404 else 503, detail=err.detail) from None


# ---------------- cart (Redis) ----------------
def cart_store():
    client = cache().client
    if client is None:
        raise HTTPException(status_code=503, detail="the cart is unavailable right now")
    return client


def cart_items(me: auth.Principal) -> dict[int, float]:
    raw = cart_store().hgetall(f"cart:{me.id}")
    return {int(k): float(v) for k, v in raw.items()}


def cart_add(me: auth.Principal, listing_id: int, quantity: float):
    key = f"cart:{me.id}"
    store = cart_store()
    store.hset(key, str(listing_id), str(quantity))
    store.expire(key, CART_TTL)


def cart_remove(me: auth.Principal, listing_id: int | None = None):
    store = cart_store()
    if listing_id is None:
        store.delete(f"cart:{me.id}")
    else:
        store.hdel(f"cart:{me.id}", str(listing_id))


# ---------------- orders ----------------
class LineIn(BaseModel):
    listing_id: int = Field(gt=0)
    quantity: float = Field(gt=0, le=1_000_000)


class OrderIn(BaseModel):
    items: list[LineIn] = Field(min_length=1, max_length=50)
    delivery_address: str = Field(min_length=5, max_length=300, pattern=TEXT)
    phone: str = Field(min_length=5, max_length=30, pattern=r"^[0-9+() \-]+$")


class StatusIn(BaseModel):
    status: str = Field(pattern="^(accepted|dispatched|delivered|cancelled)$")


def order_json(o: Order) -> dict:
    return {"id": o.id, "status": o.status, "farm": {"owner_id": o.farmer_id, "farm_name": o.farm_name},
            "buyer": {"name": o.buyer_name, "email": o.buyer_email}, "delivery_address": o.delivery_address,
            "phone": o.phone, "total": round(float(o.total), 2), "currency": o.currency, "payment": o.payment,
            "created_at": o.created_at.isoformat(),
            "items": [{"listing_id": i.listing_id, "title": i.title, "unit": i.unit, "unit_price": round(float(i.unit_price), 2),
                       "quantity": i.quantity, "line_total": round(float(i.line_total), 2)} for i in o.items]}


def release(lines: list[tuple[int, float]]):
    """Give stock back to the farm service; a failure is logged (the farmer can correct the stock by hand)."""
    for listing_id, quantity in lines:
        try:
            farm().call("POST", f"/internal/listings/{listing_id}/release", body={"quantity": quantity}, internal=True)
        except ServiceError:
            log.error("could not release %s of listing %s", quantity, listing_id)


def place(db, me: auth.Principal, data: OrderIn) -> list[Order]:
    merged: dict[int, float] = {}
    for line in data.items:
        merged[line.listing_id] = merged.get(line.listing_id, 0) + line.quantity
    reserved: list[tuple[int, float, dict]] = []
    try:
        for listing_id, quantity in merged.items():
            snap = farm().call("POST", f"/internal/listings/{listing_id}/reserve", body={"quantity": quantity}, internal=True)
            reserved.append((listing_id, quantity, snap))
            if snap["owner_id"] == me.id:
                raise HTTPException(status_code=409, detail=f"{snap['title']} is your own listing")
    except ServiceError as err:
        release([(lid, q) for lid, q, _ in reserved])
        ORDERS.labels("not-reserved").inc()
        status = 409 if err.status in (404, 409) else 503
        raise HTTPException(status_code=status, detail=f"listing {listing_id}: {err.detail}") from None
    except HTTPException:
        release([(lid, q) for lid, q, _ in reserved])
        ORDERS.labels("rejected").inc()
        raise

    by_farm: dict[str, list[tuple[int, float, dict]]] = {}
    for line in reserved:
        by_farm.setdefault(line[2]["owner_id"], []).append(line)
    orders = []
    try:
        for owner_id, lines in by_farm.items():
            first = lines[0][2]
            order = Order(buyer_id=me.id, buyer_name=me.name, buyer_email=me.email, farmer_id=owner_id,
                          farm_name=first.get("farm", {}).get("farm_name", ""), status="placed",
                          delivery_address=data.delivery_address.strip(), phone=data.phone.strip(), currency=first["currency"],
                          total=0)
            for listing_id, quantity, snap in lines:
                line_total = round(float(snap["price"]) * quantity, 2)
                order.items.append(OrderItem(listing_id=listing_id, title=snap["title"], unit=snap["unit"],
                                             unit_price=snap["price"], quantity=quantity, line_total=line_total))
                order.total = round(float(order.total) + line_total, 2)
            db.add(order)
            orders.append(order)
        db.commit()
    except Exception:
        db.rollback()
        release([(lid, q) for lid, q, _ in reserved])
        ORDERS.labels("error").inc()
        raise
    for o in orders:
        ORDERS.labels("placed").inc()
        ORDER_VALUE.labels(o.currency).inc(float(o.total))
    log.info("%d order(s) placed", len(orders))
    return orders


def change_status(db, me: auth.Principal, order_id: int, new: str, as_farmer: bool) -> Order:
    order = db.get(Order, order_id)
    owner = order.farmer_id if (order and as_farmer) else (order.buyer_id if order else None)
    if order is None or owner != me.id:
        raise HTTPException(status_code=404, detail="order not found")
    allowed = (FARMER_MOVES if as_farmer else BUYER_MOVES).get(order.status, ())
    if new not in allowed:
        raise HTTPException(status_code=409, detail=f"a {order.status} order cannot become {new}")
    order.status = new
    db.commit()
    STATUS_CHANGES.labels(new).inc()
    if new == "cancelled":
        release([(i.listing_id, i.quantity) for i in order.items])
    return order


# ---------------- pages ----------------
@app.get("/")
def home(request: Request, me: auth.Principal | None = Depends(auth.optional),
         kind: str | None = Query(None, pattern="^(produce|resource)$"), q: str | None = Query(None, max_length=60, pattern=TEXT)):
    try:
        items, error = catalog(kind, q), None
    except HTTPException as err:
        items, error = [], err.detail
    return web.page(templates, request, "catalog.html", {"me": me, "items": items, "kind": kind, "q": q or "", "error": error})


@app.get("/items/{listing_id}")
def item_page(request: Request, listing_id: int, me: auth.Principal | None = Depends(auth.optional)):
    return web.page(templates, request, "item.html", {"me": me, "item": item(listing_id)})


@app.post("/cart")
def cart_add_form(request: Request, me: auth.Principal = Depends(buyer_page), csrf: str = Form(...),
                  listing_id: int = Form(..., gt=0), quantity: float = Form(..., gt=0, le=1_000_000)):
    web.check_csrf(request, csrf)
    item(listing_id)   # it must exist
    cart_add(me, listing_id, quantity)
    return RedirectResponse("/cart", status_code=303)


@app.get("/cart")
def cart_page(request: Request, me: auth.Principal = Depends(buyer_page), error: str | None = Query(None, max_length=300)):
    lines, total, currency = [], 0.0, ""
    for listing_id, quantity in cart_items(me).items():
        try:
            it = item(listing_id)
        except HTTPException:
            continue   # no longer on sale
        line_total = round(it["price"] * quantity, 2)
        lines.append({"item": it, "quantity": quantity, "line_total": line_total})
        total, currency = total + line_total, it["currency"]
    return web.page(templates, request, "cart.html", {"me": me, "lines": lines, "total": round(total, 2), "currency": currency,
                                                      "error": error})


@app.post("/cart/remove")
def cart_remove_form(request: Request, me: auth.Principal = Depends(buyer_page), csrf: str = Form(...),
                     listing_id: int = Form(..., gt=0)):
    web.check_csrf(request, csrf)
    cart_remove(me, listing_id)
    return RedirectResponse("/cart", status_code=303)


@app.post("/checkout")
def checkout(request: Request, me: auth.Principal = Depends(buyer_page), csrf: str = Form(...),
             delivery_address: str = Form(...), phone: str = Form(...)):
    web.check_csrf(request, csrf)
    lines = [LineIn(listing_id=k, quantity=v) for k, v in cart_items(me).items()]
    try:
        data = OrderIn(items=lines, delivery_address=delivery_address, phone=phone)
        with sessions()() as db:
            orders = place(db, me, data)
    except (HTTPException, ValueError) as err:
        detail = getattr(err, "detail", None) or "check the delivery address and phone number"
        return RedirectResponse(f"/cart?error={quote(str(detail))}", status_code=303)
    cart_remove(me)
    return RedirectResponse(f"/orders?placed={len(orders)}", status_code=303)


@app.get("/orders")
def orders_page(request: Request, me: auth.Principal = Depends(buyer_page), placed: int = Query(0, ge=0, le=50),
                error: str | None = Query(None, max_length=300)):
    with sessions()() as db:
        orders = db.scalars(select(Order).where(Order.buyer_id == me.id).order_by(Order.id.desc()).limit(100)).all()
        data = [order_json(o) for o in orders]
    return web.page(templates, request, "orders.html", {"me": me, "orders": data, "placed": placed, "moves": BUYER_MOVES,
                                                        "error": error})


@app.post("/orders/{order_id}/cancel")
def cancel_form(request: Request, order_id: int, me: auth.Principal = Depends(buyer_page), csrf: str = Form(...)):
    web.check_csrf(request, csrf)
    try:
        with sessions()() as db:
            change_status(db, me, order_id, "cancelled", as_farmer=False)
    except HTTPException as err:
        return RedirectResponse(f"/orders?error={quote(str(err.detail))}", status_code=303)
    return RedirectResponse("/orders", status_code=303)


@app.get("/sales")
def sales_page(request: Request, me: auth.Principal = Depends(farmer_page), error: str | None = Query(None, max_length=300)):
    with sessions()() as db:
        orders = db.scalars(select(Order).where(Order.farmer_id == me.id).order_by(Order.id.desc()).limit(200)).all()
        data = [order_json(o) for o in orders]
    return web.page(templates, request, "sales.html", {"me": me, "orders": data, "moves": FARMER_MOVES, "error": error})


@app.post("/sales/{order_id}/status")
def sales_status_form(request: Request, order_id: int, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...),
                      status: str = Form(..., pattern="^(accepted|dispatched|delivered|cancelled)$")):
    web.check_csrf(request, csrf)
    try:
        with sessions()() as db:
            change_status(db, me, order_id, status, as_farmer=True)
    except HTTPException as err:
        return RedirectResponse(f"/sales?error={quote(str(err.detail))}", status_code=303)
    return RedirectResponse("/sales", status_code=303)


# ---------------- API ----------------
@app.get("/api/catalog")
def api_catalog(kind: str | None = Query(None, pattern="^(produce|resource)$"), q: str | None = Query(None, max_length=60, pattern=TEXT)):
    return catalog(kind, q)


@app.get("/api/items/{listing_id}")
def api_item(listing_id: int):
    return item(listing_id)


@app.post("/api/orders", status_code=201)
def api_place(body: OrderIn, me: auth.Principal = Depends(buyer)):
    with sessions()() as db:
        return [order_json(o) for o in place(db, me, body)]


@app.get("/api/orders")
def api_orders(me: auth.Principal = Depends(buyer)):
    with sessions()() as db:
        return [order_json(o) for o in db.scalars(select(Order).where(Order.buyer_id == me.id).order_by(Order.id.desc()).limit(100))]


@app.post("/api/orders/{order_id}/cancel")
def api_cancel(order_id: int, me: auth.Principal = Depends(buyer)):
    with sessions()() as db:
        return order_json(change_status(db, me, order_id, "cancelled", as_farmer=False))


@app.get("/api/sales")
def api_sales(me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return [order_json(o) for o in db.scalars(select(Order).where(Order.farmer_id == me.id).order_by(Order.id.desc()).limit(200))]


@app.post("/api/sales/{order_id}/status")
def api_sales_status(order_id: int, body: StatusIn, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return order_json(change_status(db, me, order_id, body.status, as_farmer=True))

