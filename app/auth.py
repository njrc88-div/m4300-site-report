"""Local username/password login as an access gate for this app - not a
multi-tenant identity system in the sense of per-user data (switch
credentials still live only in each browser's own localStorage,
unchanged); it's a login gate plus a role ("admin" or "user") checked
before the app's own admin routes, backed by app/users.py's SQLite store.

Entirely opt-in via SESSION_SECRET_KEY, same pattern as the Google
sign-in this replaced: unset it and AUTH_ENABLED is False, main.py adds
no session middleware and no auth gate at all, and the app runs exactly
as it did with no login of any kind. That's deliberate - it keeps
existing deployments working unchanged until someone actually sets up
SESSION_SECRET_KEY (and, for the very first admin account, the two
INITIAL_ADMIN_* vars - see users.bootstrap_initial_admin).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import audit, users

SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "")
INITIAL_ADMIN_USERNAME = os.environ.get("INITIAL_ADMIN_USERNAME", "")
INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
# Off by default because local development is plain http://localhost - a
# cookie marked Secure is simply dropped by the browser over http, which
# would silently break login rather than fail loudly. Set to "true" once
# this runs behind real https.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"

AUTH_ENABLED = bool(SESSION_SECRET_KEY)

# Reachable without a session: the login flow itself, and static assets
# (the SPA shell needs its own JS/CSS, and none of it is sensitive).
_PUBLIC_PATHS = {"/auth/login", "/auth/logout"}
_PUBLIC_PREFIXES = ("/static/",)

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def current_user(request: Request) -> dict | None:
    # request.session only exists when SessionMiddleware is installed,
    # which main.py only does when AUTH_ENABLED - guard here too so every
    # caller (not just the ones that remember to check AUTH_ENABLED first)
    # gets a clean None instead of an AssertionError.
    if not AUTH_ENABLED:
        return None
    return request.session.get("user")


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return user


def _login_page(error: str | None = None) -> str:
    error_html = f'<div class="login-error">{error}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - M4300 Site Report Generator</title>
<link rel="stylesheet" href="/static/css/style.css">
<style>
  body {{ display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .login-card {{
    background: #fff; border-radius: 10px; box-shadow: 0 4px 24px rgba(0,0,0,0.12);
    padding: 2rem 2.2rem; width: 320px;
  }}
  .login-card img {{ height: 32px; margin-bottom: 1rem; }}
  .login-card h1 {{ font-size: 1.1rem; margin: 0 0 1.2rem; color: var(--navy); }}
  .login-card label {{ display: block; font-size: 0.8rem; font-weight: 600; margin-bottom: 0.3rem; color: var(--gray-text); }}
  .login-card input {{
    width: 100%; padding: 0.5rem 0.6rem; margin-bottom: 1rem; border: 1px solid var(--gray-line);
    border-radius: 5px; font-size: 0.9rem; box-sizing: border-box;
  }}
  .login-card button {{ width: 100%; }}
  .login-error {{
    background: var(--danger-bg); color: var(--danger); border-radius: 5px;
    padding: 0.5rem 0.7rem; font-size: 0.82rem; margin-bottom: 1rem;
  }}
</style>
</head><body>
  <form class="login-card" method="post" action="/auth/login">
    <img src="/static/img/diversified-mark.png" alt="Diversified">
    <h1>M4300 Site Report Generator</h1>
    {error_html}
    <label for="username">Username</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    <button class="btn teal" type="submit">Sign in</button>
  </form>
</body></html>"""


@router.get("/login")
async def login_form(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(500, "Login isn't configured on this server (SESSION_SECRET_KEY isn't set).")
    if current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_login_page())


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not AUTH_ENABLED:
        raise HTTPException(500, "Login isn't configured on this server (SESSION_SECRET_KEY isn't set).")
    user = users.verify_password(username, password)
    if user is None:
        audit.record_event("sign_in_denied", username=username.strip(), request=request)
        return HTMLResponse(_login_page(error="Incorrect username or password."), status_code=401)
    request.session["user"] = user
    audit.record_event("sign_in", username=user["username"], request=request)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    user = current_user(request)
    if user:
        audit.record_event("sign_out", username=user["username"], request=request)
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
