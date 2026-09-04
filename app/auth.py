"""Google sign-in as an access gate for this app.

This is not a multi-tenant identity system - the app itself has no user
accounts, no per-user data, and no per-user credential storage (switch
credentials still live only in each browser's own localStorage, same as
before). A successful Google sign-in just proves "this browser belongs to
someone allowed to use this tool" and sets a signed session cookie;
that's the whole job of everything in this file.

Entirely opt-in: if GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET /
SESSION_SECRET_KEY aren't set, AUTH_ENABLED is False and main.py adds no
session middleware and no auth gate at all - the app runs exactly as it
did before this file existed. That's deliberate: it keeps every existing
deployment working unchanged until someone actually sets up a Google
Cloud OAuth client and turns this on (see README for the setup steps),
rather than a new required env var silently breaking anyone who pulls
this update without reading anything.
"""
from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import audit
from starlette.responses import JSONResponse

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "")
# Comma-separated allow-list. Google authenticates *any* Google account,
# not just ones belonging to this site - without this, "signed in with
# Google" and "allowed to use this app" aren't the same thing. Empty
# means "any successfully authenticated Google account", which is only
# reasonable if you're relying on the OAuth consent screen's own Testing
# mode (Google-side allow-list) to restrict who can complete login at all.
ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ALLOWED_GOOGLE_EMAILS", "").split(",") if e.strip()
}
# Off by default because local development is plain http://localhost -
# a cookie marked Secure is simply dropped by the browser over http, which
# would silently break login rather than fail loudly. Set to "true" once
# this runs behind real https.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"

AUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and SESSION_SECRET_KEY)

# Reachable without a session: the login flow itself, and static assets
# (the SPA shell needs its own JS/CSS to even render the "redirecting to
# Google..." moment, and none of it is sensitive on its own).
_PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/logout"}
_PUBLIC_PREFIXES = ("/static/",)

oauth = OAuth()
if AUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


@router.get("/login")
async def login(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(
            500,
            "Google sign-in isn't configured on this server (missing GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, or SESSION_SECRET_KEY).",
        )
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or await oauth.google.userinfo(token=token)
    email = (userinfo.get("email") or "").strip().lower()
    name = userinfo.get("name")
    if not email:
        raise HTTPException(403, "Google didn't return an email address for this account.")
    if not userinfo.get("email_verified", True):
        raise HTTPException(403, "This Google account's email address isn't verified.")
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        audit.record_event("sign_in_denied", email=email, name=name, request=request)
        raise HTTPException(403, f"{email} isn't authorized to use this app.")
    request.session["user"] = {
        "email": email,
        "name": name,
        "picture": userinfo.get("picture"),
    }
    audit.record_event("sign_in", email=email, name=name, request=request)
    return RedirectResponse(url="/")


@router.get("/logout")
async def logout(request: Request):
    user = current_user(request)
    if user:
        audit.record_event("sign_out", email=user["email"], name=user.get("name"), request=request)
    request.session.clear()
    return RedirectResponse(url="/auth/login")


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Redirects (or, for /api/* requests, 401s) anything without a valid
    session to /auth/login. A no-op middleware entirely when AUTH_ENABLED
    is False, so it's always safe to add to the app - see module docstring."""

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED or _is_public_path(request.url.path) or current_user(request):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated - sign in at /auth/login"}, status_code=401)
        return RedirectResponse(url="/auth/login")
