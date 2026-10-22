"""AgriPulse mobile backend: the routes behind https://api.<zone> (AWS WAF -> CloudFront -> API Gateway -> Lambda).

Farmers use the mobile app in the field: they keep a list of their fields, record crop
observations (growth stage, pests, disease, drought) and attach photos.

  GET  /health                               no auth
  GET  /fields                               the caller's fields
  POST /fields                               add a field
  GET  /fields/{field_id}/observations       observations of one of the caller's fields
  POST /fields/{field_id}/observations       record an observation (optionally with photo keys)
  POST /media/upload-url                     pre-signed S3 POST for one photo (type and size enforced by S3)

Edge: CloudFront adds X-Origin-Verify (a secret only it and this function know); without it every route but
/health answers 403, so the execute-api URL cannot be used to skip the WAF.
Identity: API Gateway's Cognito authorizer has verified the farmer's token; the farmer's id is its "sub"
claim (handler.py passes it in). Every field is owned by one farmer and every request checks the owner
(OWASP API1: broken object level authorization).
Storage: PostgreSQL on RDS (db.py). Photos: S3, scanned by GuardDuty Malware Protection and served through
CloudFront only when clean.

handle() knows nothing about Lambda: handler.py adapts the API Gateway event to it, the tests and the DAST
stand-in (app/dast/mobile_local.py) call it directly.
"""

import hmac
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from .db import FieldRow, ObservationRow, make_session_factory

log = logging.getLogger("agripulse.mobile")

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES = 64 * 1024
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
HEADERS = {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Strict-Transport-Security": "max-age=31536000"}


@dataclass
class Response:
    status: int
    body: dict | None = None
    headers: dict = field(default_factory=lambda: dict(HEADERS))

    def text(self) -> str:
        return json.dumps(self.body) if self.body is not None else ""


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ---------------- dependencies (built once per worker; replaced by the tests and the DAST stand-in) ----------------
@dataclass
class Dependencies:
    sessions: object
    s3: object
    media_bucket: str
    media_cdn_domain: str
    origin_secret: str = ""


_deps: Dependencies | None = None


def configure(deps: Dependencies | None):
    global _deps
    _deps = deps


def deps() -> Dependencies:
    global _deps
    if _deps is None:
        import boto3
        _deps = Dependencies(sessions=make_session_factory(), s3=boto3.client("s3"),
                             media_bucket=os.environ["MEDIA_BUCKET"], media_cdn_domain=os.environ.get("MEDIA_CDN_DOMAIN", ""),
                             origin_secret=os.environ.get("ORIGIN_VERIFY_SECRET", ""))
    return _deps


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- models ----------------
class FieldIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    crop: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z][A-Za-z \-]+$")
    area_ha: float = Field(gt=0, le=100_000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ObservationIn(BaseModel):
    growth_stage: Literal["planting", "emergence", "vegetative", "flowering", "fruiting", "harvest"]
    issue: Literal["none", "pest", "disease", "drought", "flood", "nutrient"]
    note: str = Field(default="", max_length=2000)
    media_keys: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("media_keys")
    @classmethod
    def own_uploads_only(cls, keys):
        for k in keys:
            if not k.startswith("uploads/") or ".." in k:
                raise ValueError("media_keys must be keys returned by /media/upload-url")
        return keys


class UploadIn(BaseModel):
    content_type: str
    size_bytes: int = Field(gt=0, le=MAX_PHOTO_BYTES)

    @field_validator("content_type")
    @classmethod
    def image_only(cls, v):
        if v not in PHOTO_TYPES:
            raise ValueError(f"content_type must be one of {sorted(PHOTO_TYPES)}")
        return v


def parse(model, raw: bytes):
    if len(raw) > MAX_BODY_BYTES:
        raise HttpError(413, "body too large")
    try:
        return model.model_validate(json.loads(raw or b"{}"))
    except ValidationError as e:
        raise HttpError(400, str(e).splitlines()[0])
    except ValueError:
        raise HttpError(400, "body must be JSON")


def field_out(f: FieldRow) -> dict:
    return {"field_id": f.id, "name": f.name, "crop": f.crop, "area_ha": f.area_ha, "latitude": f.latitude,
            "longitude": f.longitude, "created_at": f.created_at.isoformat()}


def observation_out(o: ObservationRow) -> dict:
    return {"observation_id": o.id, "created_at": o.created_at.isoformat(), "created_by": o.created_by,
            "growth_stage": o.growth_stage, "issue": o.issue, "note": o.note, "media_keys": json.loads(o.media_keys)}


# ---------------- routes ----------------
def list_fields(uid: str, _raw: bytes) -> Response:
    with deps().sessions() as db:
        rows = db.scalars(select(FieldRow).where(FieldRow.owner == uid).order_by(FieldRow.created_at)).all()
    return Response(200, {"fields": [field_out(f) for f in rows]})


def create_field(uid: str, raw: bytes) -> Response:
    data = parse(FieldIn, raw)
    row = FieldRow(id=uuid.uuid4().hex, owner=uid, created_at=now(), **data.model_dump())
    with deps().sessions() as db:
        db.add(row)
        db.commit()
    log.info("field created")
    return Response(201, field_out(row))


def owned_field(db, field_id: str, uid: str) -> FieldRow:
    """404 for both "does not exist" and "not yours": never reveal other farmers' fields."""
    row = db.get(FieldRow, field_id) if re.fullmatch(r"[0-9a-f]{32}", field_id) else None
    if row is None or row.owner != uid:
        raise HttpError(404, "field not found")
    return row


def list_observations(uid: str, _raw: bytes, field_id: str) -> Response:
    with deps().sessions() as db:
        owned_field(db, field_id, uid)
        rows = db.scalars(select(ObservationRow).where(ObservationRow.field_id == field_id)
                          .order_by(ObservationRow.created_at.desc()).limit(100)).all()
    return Response(200, {"observations": [observation_out(o) for o in rows]})


def create_observation(uid: str, raw: bytes, field_id: str) -> Response:
    with deps().sessions() as db:
        owned_field(db, field_id, uid)
        data = parse(ObservationIn, raw)
        if any(not k.startswith(f"uploads/{uid}/") for k in data.media_keys):
            raise HttpError(400, "media_keys must be your own uploads")
        row = ObservationRow(id=uuid.uuid4().hex, field_id=field_id, created_by=uid, created_at=now(),
                             growth_stage=data.growth_stage, issue=data.issue, note=data.note,
                             media_keys=json.dumps(data.media_keys))
        db.add(row)
        db.commit()
    if data.issue != "none":
        log.info("observation with issue %s", data.issue)
    return Response(201, observation_out(row))


def upload_url(uid: str, raw: bytes) -> Response:
    data = parse(UploadIn, raw)
    d = deps()
    key = f"uploads/{uid}/{uuid.uuid4().hex}.{PHOTO_TYPES[data.content_type]}"
    # S3 itself enforces the type and the maximum size; the URL expires in 5 minutes
    post = d.s3.generate_presigned_post(
        Bucket=d.media_bucket, Key=key,
        Fields={"Content-Type": data.content_type},
        Conditions=[{"Content-Type": data.content_type}, ["content-length-range", 1, MAX_PHOTO_BYTES]],
        ExpiresIn=300)
    return Response(200, {"key": key, "upload": post, "cdn_url": f"https://{d.media_cdn_domain}/{key}",
                          "note": "The photo becomes visible on the CDN after the malware scan marks it clean."})


FIELD_ID = r"(?P<field_id>[^/]{1,64})"
ROUTES = [
    ("GET", re.compile(r"^/fields$"), list_fields),
    ("POST", re.compile(r"^/fields$"), create_field),
    ("GET", re.compile(rf"^/fields/{FIELD_ID}/observations$"), list_observations),
    ("POST", re.compile(rf"^/fields/{FIELD_ID}/observations$"), create_observation),
    ("POST", re.compile(r"^/media/upload-url$"), upload_url),
]


def handle(method: str, path: str, headers: dict, body: bytes = b"", user: str | None = None) -> Response:
    """user: the Cognito "sub" API Gateway's authorizer verified (None: no valid token)."""
    headers = {k.lower(): v for k, v in headers.items()}
    path = "/" + path.strip("/")
    try:
        if path == "/health":
            if method != "GET":
                raise HttpError(405, "method not allowed")
            return Response(200, {"status": "ok"})
        d = deps()
        if d.origin_secret and not hmac.compare_digest(headers.get("x-origin-verify", ""), d.origin_secret):
            raise HttpError(403, "forbidden")
        matched = [(m, p.match(path), fn) for m, p, fn in ROUTES if p.match(path)]
        if not matched:
            raise HttpError(404, "not found")
        route = next(((rx, fn) for m, rx, fn in matched if m == method), None)
        if route is None:
            raise HttpError(405, "method not allowed")
        if not user:
            raise HttpError(401, "unauthorised")
        rx, fn = route
        return fn(user, body, **rx.groupdict())
    except HttpError as e:
        return Response(e.status, {"error": e.message})
    except Exception:
        log.exception("unhandled error")
        return Response(500, {"error": "internal error"})
