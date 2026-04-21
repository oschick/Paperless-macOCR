"""Authentication helpers for the web UI."""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING, Any

from authlib.integrations.starlette_client import OAuth
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.responses import Response

    from paperless_macocr.config import Settings

logger = logging.getLogger(__name__)

# Cookie name used for session tokens
_SESSION_COOKIE = "pmacocr_session"
_SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours

# Paths that never require authentication
_PUBLIC_PATHS = frozenset({"/health", "/webhook", "/ocr/batch", "/docs", "/openapi.json"})


def _is_public(path: str) -> bool:
    """Return True if *path* should bypass authentication."""
    if path in _PUBLIC_PATHS:
        return True
    return path.startswith("/ocr/") and not path.startswith("/ocr/preview")


def _is_web_ui(path: str) -> bool:
    """Return True if *path* is a web UI route."""
    return path.startswith("/ui") or path.startswith("/auth")


class _Signer:
    """Thin wrapper around itsdangerous for session cookie signing."""

    def __init__(self, secret: str) -> None:
        self._s = URLSafeTimedSerializer(secret)

    def sign(self, data: dict[str, Any]) -> str:
        return self._s.dumps(data)

    def unsign(self, token: str) -> dict[str, Any] | None:
        try:
            return self._s.loads(token, max_age=_SESSION_MAX_AGE)
        except BadSignature:
            return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication on web UI routes.

    API endpoints (/webhook, /ocr/*, /health) are unprotected so that
    Paperless-NGX and cron jobs can call them without credentials.
    """

    def __init__(self, app: Any, settings: Settings, signer: _Signer) -> None:
        super().__init__(app)
        self._mode = settings.web_ui_auth
        self._username = settings.web_ui_username
        self._password = settings.web_ui_password
        self._signer = signer

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Public / API paths → always allowed
        if _is_public(path) or not _is_web_ui(path):
            return await call_next(request)

        if self._mode == "none":
            return await call_next(request)

        # Auth callback path is always accessible
        if path in ("/auth/callback", "/auth/login"):
            return await call_next(request)

        # Check session cookie
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            session = self._signer.unsign(cookie)
            if session:
                request.state.user = session.get("user", "anonymous")
                return await call_next(request)

        # No valid session → redirect to login
        return RedirectResponse(f"/auth/login?next={path}")


def setup_auth(app: FastAPI, settings: Settings) -> tuple[_Signer, OAuth | None]:
    """Configure authentication middleware and return helpers."""
    from starlette.middleware.sessions import SessionMiddleware

    signer = _Signer(settings.session_secret)
    oauth: OAuth | None = None

    if settings.web_ui_auth != "none":
        app.add_middleware(AuthMiddleware, settings=settings, signer=signer)

    if settings.web_ui_auth == "oidc" and settings.oidc_discovery_url:
        # SessionMiddleware is required by authlib to store OIDC state/nonce
        # between the redirect and callback.
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
        oauth = OAuth()
        oauth.register(
            name="oidc",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=settings.oidc_discovery_url,
            client_kwargs={"scope": "openid email profile"},
        )

    return signer, oauth


def verify_basic(username: str, password: str, expected_user: str, expected_pass: str) -> bool:
    """Constant-time comparison for basic auth credentials."""
    u_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    p_ok = hmac.compare_digest(password.encode(), expected_pass.encode())
    return u_ok and p_ok
