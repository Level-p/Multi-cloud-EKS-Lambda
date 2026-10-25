"""One AgriPulse account for every service, web and mobile.

The accounts service is the only holder of the PRIVATE key: it checks the password (Argon2) and signs a short-lived
token (JWT, RS256). Every other service, and the mobile backend through them, only needs the PUBLIC key to verify
it: no shared secret, no call to the accounts service on each request.

  claims   sub (user id), email, name, role (farmer | buyer | ops), iss, aud=agripulse, iat, exp
  browser  the token is an HttpOnly, Secure, SameSite=Lax cookie on the parent domain (mfon21.space, or
           staging.mfon21.space), so signing in once covers farm., shop., incidents. of that environment.
           Staging and production use different keys and cookie names: a token of one is useless in the other.
  mobile   the token travels as "Authorization: Bearer <token>"

Roles: farmers and buyers register themselves; "ops" (the incident blog) is granted by an operator
(python -m app.admin grant-ops <email> in the accounts pod), never through a public endpoint.
"""

import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import jwt
from fastapi import HTTPException, Request

AUDIENCE = "agripulse"
ROLES = ("farmer", "buyer", "ops")
ALGORITHM = "RS256"


@dataclass(frozen=True)
class Principal:
    id: str
    email: str
    name: str
    role: str


class LoginRequired(Exception):
    """Raised by HTML pages: the app turns it into a redirect to the accounts sign-in page."""

    def __init__(self, login_url: str):
        self.login_url = login_url


def cookie_name() -> str:
    return os.environ.get("SESSION_COOKIE", "agripulse_session")


def issuer() -> str:
    return os.environ.get("JWT_ISSUER", "agripulse-accounts")


def token_ttl() -> int:
    return int(os.environ.get("TOKEN_TTL_SECONDS", str(8 * 3600)))


def issue_token(private_key_pem: str, *, user_id: str, email: str, name: str, role: str, now: float | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    iat = int(now if now is not None else time.time())
    claims = {"sub": user_id, "email": email, "name": name, "role": role, "iss": issuer(), "aud": AUDIENCE,
              "iat": iat, "exp": iat + token_ttl(), "jti": uuid.uuid4().hex}
    return jwt.encode(claims, private_key_pem, algorithm=ALGORITHM)


def verify_token(token: str, public_key_pem: str) -> Principal:
    """The principal, or jwt.PyJWTError (expired, wrong key, wrong issuer/audience, unknown role)."""
    claims = jwt.decode(token, public_key_pem, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=issuer(),
                        options={"require": ["sub", "exp", "iat", "iss", "aud"]})
    if claims.get("role") not in ROLES:
        raise jwt.InvalidTokenError("unknown role")
    return Principal(id=str(claims["sub"]), email=str(claims.get("email", "")), name=str(claims.get("name", "")),
                     role=claims["role"])


def public_key() -> str:
    key = os.environ.get("JWT_PUBLIC_KEY", "")
    if not key:
        raise RuntimeError("JWT_PUBLIC_KEY is not set")
    return key


def token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and len(auth) > 7:
        return auth[7:].strip()
    return request.cookies.get(cookie_name())


def principal_from(request: Request) -> Principal | None:
    token = token_from(request)
    if not token:
        return None
    try:
        return verify_token(token, public_key())
    except jwt.PyJWTError:
        return None


def login_url(request: Request) -> str:
    """The accounts sign-in page, coming back to this page afterwards."""
    here = os.environ.get("PUBLIC_URL", "").rstrip("/") + request.url.path
    if request.url.query:
        here += "?" + request.url.query
    return f"{os.environ.get('ACCOUNTS_URL', '').rstrip('/')}/login?next={quote(here, safe='')}"


def requires(*roles: str, html: bool = False):
    """FastAPI dependency: the signed-in principal, with one of the roles if given.
    html=True: a visitor without a session is sent to the sign-in page instead of getting 401."""

    def dependency(request: Request) -> Principal:
        principal = principal_from(request)
        if principal is None:
            if html:
                raise LoginRequired(login_url(request))
            raise HTTPException(status_code=401, detail="sign in required", headers={"WWW-Authenticate": "Bearer"})
        if roles and principal.role not in roles:
            raise HTTPException(status_code=403, detail=f"this needs a {' or '.join(roles)} account")
        return principal

    return dependency


def optional(request: Request) -> Principal | None:
    """FastAPI dependency for public pages that show more to signed-in visitors."""
    return principal_from(request)
