"""AgriPulse accounts: sign-up and sign-in for farmers and buyers. One account works on the web and in the mobile app.

  GET  /                  your account, or links to sign in / register
  GET  /register          form (I am a farmer / a buyer)          POST /register
  GET  /login             form                                    POST /login
  GET  /logout           confirm (a button: signing out is a form post)  POST /logout
  POST /api/register      JSON {email, password, name, role, phone?} -> {token, expires_in, user}   (mobile app)
  POST /api/token         JSON {email, password}                   -> {token, expires_in, user}   (mobile app)
  GET  /api/me            the caller
  GET  /healthz /readyz /metrics

Passwords: Argon2id (argon2-cffi), 10-128 characters, never logged. Five wrong passwords lock the account for
15 minutes (and AWS WAF rate-limits the sign-in pages per IP). A wrong e-mail and a wrong password give the same
answer and take the same time, so the sign-in cannot be used to find out who has an account.
Only farmer and buyer can be chosen at sign-up; "ops" is granted by an operator: python -m app.admin grant-ops <email>.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from prometheus_client import Counter
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from agripulse_common import auth, web
from agripulse_common.db import make_session_factory, ping

from .db import Base, User

log = logging.getLogger("agripulse.accounts")
SIGNUPS = Counter("agripulse_signups_total", "Accounts created", ["role"])
LOGINS = Counter("agripulse_logins_total", "Sign-in attempts", ["result"])
SELF_SERVICE_ROLES = ("farmer", "buyer")
MAX_FAILED, LOCK_MINUTES = 5, 15
hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash("timing-equaliser-not-a-password")
_sessions = None


def sessions():
    global _sessions
    if _sessions is None:
        _sessions = make_session_factory(Base.metadata)
    return _sessions


app, templates = web.create_app("accounts", Path(__file__).parent, readiness=lambda: ping(sessions),
                                loggers=("agripulse", "uvicorn.error"))


# ---------------- tokens and the session cookie ----------------
def private_key() -> str:
    key = os.environ.get("JWT_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("JWT_PRIVATE_KEY is not set")
    return key


def token_for(user: User) -> str:
    return auth.issue_token(private_key(), user_id=user.id, email=user.email, name=user.name, role=user.role)


def user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "phone": user.phone}


def set_session(response, token: str):
    domain = os.environ.get("COOKIE_DOMAIN") or None
    response.set_cookie(auth.cookie_name(), token, max_age=auth.token_ttl(), httponly=True, secure=web.cookie_secure(),
                        samesite="lax", domain=domain, path="/")


def home_for(role: str) -> str:
    return {"farmer": os.environ.get("FARM_URL", "") + "/dashboard", "buyer": os.environ.get("SHOP_URL", "") + "/",
            "ops": os.environ.get("INCIDENTS_URL", "") + "/"}.get(role, "/")


def safe_next(target: str | None, default: str) -> str:
    """Only back to a page of AgriPulse itself (an open redirect would help phishing)."""
    if not target:
        return default
    domain = os.environ.get("COOKIE_DOMAIN", "")
    parts = urlsplit(target)
    if parts.scheme == "https" and domain and parts.hostname and (parts.hostname == domain or parts.hostname.endswith("." + domain)):
        return target
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


# ---------------- the two operations ----------------
def register(db, email: str, password: str, name: str, role: str, phone: str = "") -> User:
    if role not in SELF_SERVICE_ROLES:
        raise HTTPException(status_code=422, detail="role must be farmer or buyer")
    email = email.strip().lower()
    if db.scalar(select(func.count()).select_from(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="an account with this e-mail already exists: sign in instead")
    user = User(email=email, name=name.strip(), role=role, phone=phone.strip(), password_hash=hasher.hash(password))
    db.add(user)
    db.commit()
    SIGNUPS.labels(role).inc()
    log.info("account created: role %s", role)
    return user


def authenticate(db, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    now = datetime.now(timezone.utc)
    if user is None:
        try:
            hasher.verify(_DUMMY_HASH, password)   # same work as a real check: no timing difference
        except VerificationError:
            pass
        LOGINS.labels("unknown").inc()
        raise HTTPException(status_code=401, detail="wrong e-mail or password")
    if user.locked_until and _aware(user.locked_until) > now:
        LOGINS.labels("locked").inc()
        raise HTTPException(status_code=429, detail="too many wrong passwords: try again in 15 minutes")
    try:
        hasher.verify(user.password_hash, password)
    except (VerificationError, InvalidHashError):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until, user.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
        db.commit()
        LOGINS.labels("wrong-password").inc()
        raise HTTPException(status_code=401, detail="wrong e-mail or password")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(password)
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    db.commit()
    LOGINS.labels("ok").inc()
    return user


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------- pages ----------------
@app.get("/")
def account_page(request: Request, me: auth.Principal | None = Depends(auth.optional)):
    return web.page(templates, request, "account.html", {"me": me, "home": home_for(me.role) if me else None})


@app.get("/register")
def register_form(request: Request, role: str = "buyer", next: str | None = None):
    return web.page(templates, request, "register.html", {"role": role if role in SELF_SERVICE_ROLES else "buyer",
                                                          "next": next or "", "error": None})


@app.post("/register")
def register_submit(request: Request, csrf: str = Form(...), name: str = Form(..., min_length=2, max_length=100),
                    email: EmailStr = Form(...), password: str = Form(..., min_length=10, max_length=128),
                    role: str = Form(..., pattern="^(farmer|buyer)$"), phone: str = Form("", max_length=30),
                    next: str = Form("", max_length=500)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        try:
            user = register(db, str(email), password, name, role, phone)
        except HTTPException as err:
            return web.page(templates, request, "register.html", {"role": role, "next": next, "error": err.detail},
                            status_code=err.status_code)
        response = RedirectResponse(safe_next(next, home_for(user.role)), status_code=303)
        set_session(response, token_for(user))
        return response


@app.get("/login")
def login_form(request: Request, next: str | None = None):
    return web.page(templates, request, "login.html", {"next": next or "", "error": None})


@app.post("/login")
def login_submit(request: Request, csrf: str = Form(...), email: str = Form(..., max_length=254),
                 password: str = Form(..., max_length=128), next: str = Form("", max_length=500)):
    web.check_csrf(request, csrf)
    with sessions()() as db:
        try:
            user = authenticate(db, email, password)
        except HTTPException as err:
            return web.page(templates, request, "login.html", {"next": next, "error": err.detail}, status_code=err.status_code)
        response = RedirectResponse(safe_next(next, home_for(user.role)), status_code=303)
        set_session(response, token_for(user))
        return response


@app.get("/logout")
def logout_form(request: Request, me: auth.Principal | None = Depends(auth.optional)):
    return web.page(templates, request, "logout.html", {"me": me})


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    web.check_csrf(request, csrf)
    response = RedirectResponse(os.environ.get("SHOP_URL", "") + "/", status_code=303)
    response.delete_cookie(auth.cookie_name(), domain=os.environ.get("COOKIE_DOMAIN") or None, path="/")
    return response


# ---------------- API (mobile app) ----------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    name: str = Field(min_length=2, max_length=100)
    role: str = Field(pattern="^(farmer|buyer)$")
    phone: str = Field(default="", max_length=30)


class TokenIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


def token_answer(user: User, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"token": token_for(user), "token_type": "Bearer", "expires_in": auth.token_ttl(),
                         "user": user_json(user)}, status_code=status_code)


@app.post("/api/register", status_code=201)
def api_register(body: RegisterIn):
    with sessions()() as db:
        return token_answer(register(db, str(body.email), body.password, body.name, body.role, body.phone), 201)


@app.post("/api/token")
def api_token(body: TokenIn):
    with sessions()() as db:
        return token_answer(authenticate(db, body.email, body.password))


@app.get("/api/me")
def api_me(me: auth.Principal = Depends(auth.requires())):
    with sessions()() as db:
        user = db.get(User, me.id)
        if user is None:
            raise HTTPException(status_code=404, detail="account not found")
        return user_json(user)
