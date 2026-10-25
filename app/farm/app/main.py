"""AgriPulse farm: the farmers' side. Farmers register (accounts service), then describe their farm, list what they sell
and keep their farm records here. Buyers see the listings in the shop and in the mobile app.

Pages (farmers)
  GET  /                          what AgriPulse offers farmers (signed-in farmers go to their dashboard)
  GET  /dashboard                 profile, listings, farm records, links to sales (shop)
  POST /profile                   create or update the farm profile (location, size, monthly capacity)
  POST /listings                  list produce or a farm resource           POST /listings/{id}/update, /delete
  GET  /listings/{id}/photo       upload a photo straight to S3 (pre-signed form, scanned by GuardDuty)
  POST /activities                add a farm record (planting, spraying, harvest...)
  GET  /farms/{owner_id}          a farm's public page with what it sells
API (mobile app, Bearer token)
  GET|PUT  /api/profile           GET|POST /api/listings     PATCH|DELETE /api/listings/{id}
  POST /api/listings/{id}/photo   GET|POST /api/activities
  GET  /api/catalog               PUBLIC: what is for sale (kind, q, region, limit, offset)
  GET  /api/listings/{id}         PUBLIC          GET /api/farms/{owner_id}   PUBLIC
Internal (shop only, inside the cluster)
  POST /internal/listings/{id}/reserve   take stock for an order (atomic: never below zero)
  POST /internal/listings/{id}/release   give it back (order cancelled)
"""

import logging
import os
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update

from agripulse_common import auth, web
from agripulse_common.db import make_session_factory, ping
from agripulse_common.services import require_internal

from .db import Activity, Base, Farm, Listing

log = logging.getLogger("agripulse.farm")
RESERVATIONS = Counter("agripulse_stock_reservations_total", "Stock reservations from orders", ["result"])
LISTINGS = Counter("agripulse_listings_created_total", "Listings created", ["kind"])

KINDS = ("produce", "resource")
UNITS = ("kg", "tonne", "crate", "bag", "basket", "bunch", "piece", "dozen", "litre", "tuber", "day", "hour")
ACTIVITY_KINDS = ("planting", "fertilising", "irrigation", "spraying", "weeding", "harvest", "sale", "livestock", "other")
TEXT = r"^[^<>]*$"   # names and short texts: no markup (the pages escape anyway; this keeps the data clean)
_sessions = None
_s3 = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory(Base.metadata)
    return _sessions


def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
    return _s3


app, templates = web.create_app("farm", Path(__file__).parent, readiness=lambda: ping(sessions),
                                loggers=("agripulse", "uvicorn.error"))
farmer = auth.requires("farmer")
farmer_page = auth.requires("farmer", html=True)


def currency() -> str:
    return os.environ.get("CURRENCY", "NGN")


def media_url(key: str | None) -> str | None:
    domain = os.environ.get("MEDIA_CDN_DOMAIN")
    return f"https://{domain}/{key}" if key and domain else None


# ---------------- shapes ----------------
class ProfileIn(BaseModel):
    farm_name: str = Field(min_length=2, max_length=100, pattern=TEXT)
    phone: str = Field(default="", max_length=30, pattern=r"^[0-9+() \-]*$")
    region: str = Field(min_length=2, max_length=60, pattern=TEXT)
    address: str = Field(default="", max_length=200, pattern=TEXT)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    hectares: float = Field(gt=0, le=100_000)
    capacity_tonnes_month: float = Field(ge=0, le=1_000_000)
    description: str = Field(default="", max_length=2000)


class ListingIn(BaseModel):
    title: str = Field(min_length=2, max_length=100, pattern=TEXT)
    kind: str = Field(pattern="^(produce|resource)$")
    category: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z][A-Za-z &\-]*$")
    unit: str = Field(pattern="^(" + "|".join(UNITS) + ")$")
    price: float = Field(gt=0, le=100_000_000)
    quantity: float = Field(ge=0, le=10_000_000)
    description: str = Field(default="", max_length=2000)


class ListingPatch(BaseModel):
    price: float | None = Field(default=None, gt=0, le=100_000_000)
    quantity: float | None = Field(default=None, ge=0, le=10_000_000)
    active: bool | None = None
    description: str | None = Field(default=None, max_length=2000)


class ActivityIn(BaseModel):
    on: date
    kind: str = Field(pattern="^(" + "|".join(ACTIVITY_KINDS) + ")$")
    crop: str = Field(default="", max_length=60, pattern=TEXT)
    quantity: float | None = Field(default=None, ge=0, le=10_000_000)
    unit: str = Field(default="", pattern="^(" + "|".join(UNITS) + ")?$")
    notes: str = Field(default="", max_length=2000)


class QuantityIn(BaseModel):
    quantity: float = Field(gt=0, le=10_000_000)


def farm_json(f: Farm) -> dict:
    return {"owner_id": f.owner_id, "farm_name": f.farm_name, "owner_name": f.owner_name, "phone": f.phone,
            "region": f.region, "address": f.address, "latitude": f.latitude, "longitude": f.longitude,
            "hectares": f.hectares, "capacity_tonnes_month": f.capacity_tonnes_month, "description": f.description}


def listing_json(item: Listing, farm: Farm | None = None) -> dict:
    data = {"id": item.id, "owner_id": item.owner_id, "title": item.title, "kind": item.kind, "category": item.category,
            "unit": item.unit, "price": round(float(item.price), 2), "currency": item.currency, "quantity": item.quantity,
            "description": item.description, "image_url": media_url(item.image_key), "active": item.active}
    if farm:
        data["farm"] = {"owner_id": farm.owner_id, "farm_name": farm.farm_name, "region": farm.region}
    return data


def activity_json(a: Activity) -> dict:
    return {"id": a.id, "on": a.on.isoformat(), "kind": a.kind, "crop": a.crop, "quantity": a.quantity, "unit": a.unit,
            "notes": a.notes}


# ---------------- operations (shared by the pages and the API) ----------------
def save_profile(db, me: auth.Principal, data: ProfileIn) -> Farm:
    farm = db.get(Farm, me.id) or Farm(owner_id=me.id)
    for k, v in data.model_dump().items():
        setattr(farm, k, v)
    farm.owner_name = me.name
    db.add(farm)
    db.commit()
    return farm


def create_listing(db, me: auth.Principal, data: ListingIn) -> Listing:
    if db.get(Farm, me.id) is None:
        raise HTTPException(status_code=409, detail="create your farm profile first")
    item = Listing(owner_id=me.id, currency=currency(), **data.model_dump())
    db.add(item)
    db.commit()
    LISTINGS.labels(item.kind).inc()
    return item


def own_listing(db, me: auth.Principal, listing_id: int) -> Listing:
    item = db.get(Listing, listing_id)
    if item is None or item.owner_id != me.id:   # someone else's listing looks the same as a missing one
        raise HTTPException(status_code=404, detail="listing not found")
    return item


def patch_listing(db, me: auth.Principal, listing_id: int, data: ListingPatch) -> Listing:
    item = own_listing(db, me, listing_id)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(item, k, v)
    db.commit()
    return item


def photo_upload(db, me: auth.Principal, listing_id: int, redirect: str | None = None) -> dict:
    """A pre-signed S3 POST for one photo of the listing: JPEG/PNG/WebP, at most 5 MB, under uploads/<owner>/.
    GuardDuty scans it; the CDN serves it only once it is tagged clean (bucket policy, infra/modules/edge)."""
    item = own_listing(db, me, listing_id)
    bucket = os.environ.get("MEDIA_BUCKET")
    if not bucket:
        raise HTTPException(status_code=503, detail="photo uploads are not configured")
    key = f"uploads/{me.id}/{uuid.uuid4().hex}"
    fields = {"success_action_redirect": redirect} if redirect else {}
    conditions = [["content-length-range", 1, 5 * 1024 * 1024], ["starts-with", "$Content-Type", "image/"]]
    if redirect:
        conditions.append({"success_action_redirect": redirect})
    post = s3().generate_presigned_post(bucket, key, Fields=fields, Conditions=conditions, ExpiresIn=600)
    item.image_key = key
    db.commit()
    return {"url": post["url"], "fields": post["fields"], "key": key, "image_url": media_url(key)}


def add_activity(db, me: auth.Principal, data: ActivityIn) -> Activity:
    act = Activity(owner_id=me.id, **data.model_dump())
    db.add(act)
    db.commit()
    return act


def catalog(db, kind: str | None, q: str | None, region: str | None, limit: int, offset: int) -> list[dict]:
    stmt = (select(Listing, Farm).join(Farm, Farm.owner_id == Listing.owner_id)
            .where(Listing.active.is_(True), Listing.quantity > 0).order_by(Listing.updated_at.desc(), Listing.id.desc()))
    if kind:
        stmt = stmt.where(Listing.kind == kind)
    if region:
        stmt = stmt.where(Farm.region == region)
    if q:
        like = f"%{q.lower()}%"   # bound parameter: the text is data, never SQL
        stmt = stmt.where(or_(Listing.title.ilike(like), Listing.category.ilike(like), Farm.farm_name.ilike(like)))
    return [listing_json(item, farm) for item, farm in db.execute(stmt.limit(limit).offset(offset)).all()]


# ---------------- pages ----------------
@app.get("/")
def landing(request: Request, me: auth.Principal | None = Depends(auth.optional)):
    if me and me.role == "farmer":
        return RedirectResponse("/dashboard", status_code=303)
    return web.page(templates, request, "landing.html", {"me": me})


@app.get("/dashboard")
def dashboard(request: Request, me: auth.Principal = Depends(farmer_page), notice: str | None = Query(None, max_length=120),
              error: str | None = Query(None, max_length=300)):
    with sessions()() as db:
        farm = db.get(Farm, me.id)
        items = db.scalars(select(Listing).where(Listing.owner_id == me.id).order_by(Listing.id.desc())).all()
        acts = db.scalars(select(Activity).where(Activity.owner_id == me.id).order_by(Activity.on.desc(), Activity.id.desc())
                          .limit(50)).all()
    return web.page(templates, request, "dashboard.html", {
        "me": me, "farm": farm, "listings": [listing_json(i) for i in items], "activities": acts, "units": UNITS,
        "activity_kinds": ACTIVITY_KINDS, "today": date.today().isoformat(), "currency": currency(), "notice": notice,
        "error": error})


class FormProblem(Exception):
    """A form the farmer filled in wrongly: back to the dashboard with the reason shown."""


@app.exception_handler(FormProblem)
async def form_problem(request: Request, exc: FormProblem):
    return RedirectResponse(f"/dashboard?error={quote(str(exc))}", status_code=303)


def form_model(model, **values):
    try:
        return model(**values)
    except ValueError as err:   # pydantic.ValidationError: the first problem, in words
        errors = getattr(err, "errors", None)
        first = errors()[0] if errors else {"loc": ["form"], "msg": str(err)}
        raise FormProblem(f"{first['loc'][-1]}: {first['msg']}") from None


def form_action(fn):
    """Business errors of a form (no profile yet, not found...) also go back to the dashboard."""
    try:
        return fn()
    except HTTPException as err:
        raise FormProblem(str(err.detail)) from None


@app.post("/profile")
def profile_form(request: Request, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...),
                 farm_name: str = Form(...), phone: str = Form(""), region: str = Form(...), address: str = Form(""),
                 latitude: str = Form(""), longitude: str = Form(""), hectares: str = Form(...),
                 capacity_tonnes_month: str = Form(...), description: str = Form("")):
    web.check_csrf(request, csrf)
    data = form_model(ProfileIn, farm_name=farm_name, phone=phone, region=region, address=address,
                      latitude=latitude.strip() or None, longitude=longitude.strip() or None, hectares=hectares,
                      capacity_tonnes_month=capacity_tonnes_month, description=description)
    with sessions()() as db:
        save_profile(db, me, data)
    return RedirectResponse("/dashboard?notice=Profile+saved", status_code=303)


@app.post("/listings")
def listing_form(request: Request, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...), title: str = Form(...),
                 kind: str = Form(...), category: str = Form(...), unit: str = Form(...), price: str = Form(...),
                 quantity: str = Form(...), description: str = Form("")):
    web.check_csrf(request, csrf)
    data = form_model(ListingIn, title=title, kind=kind, category=category, unit=unit, price=price, quantity=quantity,
                      description=description)
    with sessions()() as db:
        form_action(lambda: create_listing(db, me, data))
    return RedirectResponse("/dashboard?notice=Listing+added", status_code=303)


@app.post("/listings/{listing_id}/update")
def listing_update_form(request: Request, listing_id: int, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...),
                        price: str = Form(...), quantity: str = Form(...), active: str = Form("off")):
    web.check_csrf(request, csrf)
    data = form_model(ListingPatch, price=price, quantity=quantity, active=active == "on")
    with sessions()() as db:
        form_action(lambda: patch_listing(db, me, listing_id, data))
    return RedirectResponse("/dashboard?notice=Listing+updated", status_code=303)


@app.post("/listings/{listing_id}/delete")
def listing_delete_form(request: Request, listing_id: int, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        form_action(lambda: own_listing(db, me, listing_id)).active = False   # kept for past orders; hidden from the shop
        db.commit()
    return RedirectResponse("/dashboard?notice=Listing+removed", status_code=303)


@app.get("/listings/{listing_id}/photo")
def photo_form(request: Request, listing_id: int, me: auth.Principal = Depends(farmer_page)):
    back = os.environ.get("PUBLIC_URL", "").rstrip("/") + "/dashboard?notice=Photo+uploaded:+it+appears+once+scanned"
    with sessions()() as db:
        upload = photo_upload(db, me, listing_id, redirect=back)
        item = own_listing(db, me, listing_id)
    return web.page(templates, request, "photo.html", {"me": me, "upload": upload, "item": item})


@app.post("/activities")
def activity_form(request: Request, me: auth.Principal = Depends(farmer_page), csrf: str = Form(...),
                  on: str = Form(...), kind: str = Form(...), crop: str = Form(""), quantity: str = Form(""),
                  unit: str = Form(""), notes: str = Form("")):
    web.check_csrf(request, csrf)
    data = form_model(ActivityIn, on=on, kind=kind, crop=crop, quantity=quantity.strip() or None, unit=unit, notes=notes)
    with sessions()() as db:
        add_activity(db, me, data)
    return RedirectResponse("/dashboard?notice=Record+added", status_code=303)


@app.get("/farms/{owner_id}")
def farm_page(request: Request, owner_id: str, me: auth.Principal | None = Depends(auth.optional)):
    with sessions()() as db:
        farm = db.get(Farm, owner_id[:36])
        if farm is None:
            raise HTTPException(status_code=404, detail="farm not found")
        items = db.scalars(select(Listing).where(Listing.owner_id == farm.owner_id, Listing.active.is_(True),
                                                 Listing.quantity > 0)).all()
    return web.page(templates, request, "farm.html", {"me": me, "farm": farm, "listings": [listing_json(i) for i in items]})


# ---------------- API ----------------
@app.get("/api/profile")
def api_profile(me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        farm = db.get(Farm, me.id)
        if farm is None:
            raise HTTPException(status_code=404, detail="no farm profile yet")
        return farm_json(farm)


@app.put("/api/profile")
def api_save_profile(body: ProfileIn, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return farm_json(save_profile(db, me, body))


@app.get("/api/listings")
def api_my_listings(me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return [listing_json(i) for i in db.scalars(select(Listing).where(Listing.owner_id == me.id).order_by(Listing.id))]


@app.post("/api/listings", status_code=201)
def api_create_listing(body: ListingIn, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return listing_json(create_listing(db, me, body))


@app.patch("/api/listings/{listing_id}")
def api_patch_listing(listing_id: int, body: ListingPatch, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return listing_json(patch_listing(db, me, listing_id, body))


@app.delete("/api/listings/{listing_id}", status_code=204)
def api_delete_listing(listing_id: int, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        own_listing(db, me, listing_id).active = False
        db.commit()


@app.post("/api/listings/{listing_id}/photo")
def api_photo(listing_id: int, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return photo_upload(db, me, listing_id)


@app.get("/api/activities")
def api_activities(me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return [activity_json(a) for a in db.scalars(select(Activity).where(Activity.owner_id == me.id)
                                                     .order_by(Activity.on.desc(), Activity.id.desc()))]


@app.post("/api/activities", status_code=201)
def api_add_activity(body: ActivityIn, me: auth.Principal = Depends(farmer)):
    with sessions()() as db:
        return activity_json(add_activity(db, me, body))


@app.get("/api/catalog")
def api_catalog(kind: str | None = Query(None, pattern="^(produce|resource)$"),
                q: str | None = Query(None, max_length=60, pattern=TEXT), region: str | None = Query(None, max_length=60, pattern=TEXT),
                limit: int = Query(48, ge=1, le=100), offset: int = Query(0, ge=0, le=10_000)):
    with sessions()() as db:
        return catalog(db, kind, q, region, limit, offset)


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int):
    with sessions()() as db:
        item = db.get(Listing, listing_id)
        if item is None or not item.active:
            raise HTTPException(status_code=404, detail="listing not found")
        return listing_json(item, db.get(Farm, item.owner_id))


@app.get("/api/farms/{owner_id}")
def api_farm(owner_id: str):
    with sessions()() as db:
        farm = db.get(Farm, owner_id[:36])
        if farm is None:
            raise HTTPException(status_code=404, detail="farm not found")
        data = farm_json(farm)
        data.pop("phone")   # contact details only through orders
        return data


# ---------------- internal (the shop) ----------------
@app.post("/internal/listings/{listing_id}/reserve", dependencies=[Depends(require_internal)])
def reserve(listing_id: int, body: QuantityIn):
    with sessions()() as db:
        # One conditional UPDATE: two orders racing for the last crate cannot both get it
        done = db.execute(update(Listing).where(Listing.id == listing_id, Listing.active.is_(True),
                                                Listing.quantity >= body.quantity)
                          .values(quantity=Listing.quantity - body.quantity)).rowcount
        db.commit()
        item = db.get(Listing, listing_id)
        if not done or item is None:
            RESERVATIONS.labels("insufficient").inc()
            raise HTTPException(status_code=409, detail="not enough in stock" if item else "listing not found")
        RESERVATIONS.labels("ok").inc()
        return listing_json(item, db.get(Farm, item.owner_id))


@app.post("/internal/listings/{listing_id}/release", dependencies=[Depends(require_internal)])
def release(listing_id: int, body: QuantityIn):
    with sessions()() as db:
        db.execute(update(Listing).where(Listing.id == listing_id).values(quantity=Listing.quantity + body.quantity))
        db.commit()
        RESERVATIONS.labels("released").inc()
        return {"released": body.quantity}
