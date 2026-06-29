"""Login / logout routes for the monitoring UI (design §19.5, M7.4).

Single-admin auth: GET /login renders the form with a fresh CSRF token; POST /login verifies
CSRF + lockout + argon2id password (constant-time even for an unknown user, to avoid
user-enumeration timing), sets the signed session cookie on success, and re-renders a GENERIC
error on failure (never reveals whether the user or the password was wrong). POST /logout
clears the cookie. Auth events are logged to the web's own stdout logger — never the trading DB.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from trader.web.auth import clear_session_cookie, set_session_cookie
from trader.web.security import (
    LoginThrottle,
    make_csrf_token,
    make_session_token,
    validate_csrf,
    verify_password,
)
from trader.web.settings import WebSettings

logger = logging.getLogger("trader.web")
router = APIRouter()

# A REAL argon2id hash of a throwaway string: verified against when the username is unknown so
# a bad-username attempt does the SAME full argon2 work as a bad-password attempt (no
# user-enumeration timing side-channel). Must be a valid hash or verify would fail-fast.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$vUAUcIETh49seyTuVCkoXw$"
    "nP6/yiB2PCoQmHAv4qt/hyQw5caLaLmeTeEZxiNIWoA"
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _render_login(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> Response:
    settings: WebSettings = request.app.state.settings
    now = request.app.state.now()
    templates: Jinja2Templates = request.app.state.templates
    csrf = make_csrf_token(settings.session_secret.get_secret_value(), now)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf, "error": error},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    return _render_login(request)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(""),
) -> Response:
    settings: WebSettings = request.app.state.settings
    secret = settings.session_secret.get_secret_value()
    now = request.app.state.now()
    throttle: LoginThrottle = request.app.state.login_throttle
    ip = _client_ip(request)

    if not validate_csrf(secret, csrf):
        raise HTTPException(status_code=403, detail="invalid CSRF token")

    key = LoginThrottle.key(username, ip)
    if throttle.is_locked(key, now):
        logger.warning("login lockout: user=%s ip=%s", username, ip)
        return _render_login(request, error="Too many attempts. Try again later.", status_code=429)

    # Constant-time-ish: always run a verify, against the dummy hash for an unknown user.
    if username == settings.admin_user:
        ok = verify_password(password, settings.admin_password_hash.get_secret_value())
    else:
        verify_password(password, _DUMMY_HASH)
        ok = False

    if not ok:
        throttle.record_failure(key, now)
        logger.warning("login failed: user=%s ip=%s", username, ip)
        return _render_login(request, error="Invalid credentials.", status_code=401)

    throttle.record_success(key)
    logger.info("login success: user=%s ip=%s", username, ip)
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, make_session_token(secret, username, now), settings)
    return response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")) -> RedirectResponse:
    settings: WebSettings = request.app.state.settings
    if not validate_csrf(settings.session_secret.get_secret_value(), csrf):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


__all__ = ["router"]
