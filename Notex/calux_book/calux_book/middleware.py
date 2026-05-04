"""Middleware for Calux Book — audit logging and auth."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .auth import decode_jwt

logger = logging.getLogger("calux_book.middleware")

# ---------------------------------------------------------------------------
# Audit logger setup
# ---------------------------------------------------------------------------

_audit_logger: logging.Logger | None = None


def _get_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    Path("./logs").mkdir(parents=True, exist_ok=True)

    _audit_logger = logging.getLogger("calux_book.audit")
    _audit_logger.setLevel(logging.INFO)
    _audit_logger.propagate = False

    handler = TimedRotatingFileHandler(
        filename="./logs/audit.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    handler.suffix = "%Y%m%d"
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    _audit_logger.addHandler(handler)

    # Also log to stdout
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    _audit_logger.addHandler(console)

    return _audit_logger


def log_user_activity(
    action: str, user_id: str, resource_type: str, resource_id: str,
    resource_name: str, details: str, ip: str, ua: str,
) -> None:
    audit = _get_audit_logger()
    audit.info(
        "[USER_ACTIVITY] action=%s user_id=%s resource_type=%s resource_id=%s "
        "resource_name=%r details=%r ip=%s user_agent=%r",
        action, user_id, resource_type, resource_id, resource_name, details, ip, ua,
    )


# ---------------------------------------------------------------------------
# Client IP extraction
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    for header in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip"):
        val = request.headers.get(header)
        if val:
            return val.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Audit middleware
# ---------------------------------------------------------------------------

class AuditMiddleware(BaseHTTPMiddleware):
    """Lightweight request audit logging."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        latency_ms = int((time.monotonic() - start) * 1000)
        ip = get_client_ip(request)
        audit = _get_audit_logger()
        audit.info(
            "[AUDIT] client_ip=%s method=%s path=%s status=%d latency_ms=%d",
            ip, request.method, request.url.path, response.status_code, latency_ms,
        )
        return response


# ---------------------------------------------------------------------------
# Auth helpers for route handlers
# ---------------------------------------------------------------------------

def extract_user_id(request: Request, jwt_secret: str) -> str:
    """Extract user_id from JWT (header, cookie, or query param).

    Falls back to a stable guest identity stored in a cookie so the app
    works end-to-end without mandatory OAuth login.
    """
    token = _extract_token(request)
    if token:
        claims = decode_jwt(token, jwt_secret)
        if claims and "user_id" in claims:
            return claims["user_id"]

    # Guest fallback — use cookie-based stable identity
    guest_id = request.cookies.get("calux_guest_id", "")
    if guest_id:
        return f"guest:{guest_id}"

    return f"guest:{uuid4()}"


def extract_user_id_optional(request: Request, jwt_secret: str) -> str:
    """Like ``extract_user_id`` but returns empty string when no token is found."""
    token = _extract_token(request)
    if token:
        claims = decode_jwt(token, jwt_secret)
        if claims and "user_id" in claims:
            return claims["user_id"]
    return ""


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    if auth:
        return auth
    token = request.cookies.get("token", "")
    if token:
        return token
    return request.query_params.get("token", "")


def set_guest_cookie(response: Response, user_id: str) -> None:
    """Persist guest identity cookie when a new guest ID was generated."""
    if user_id.startswith("guest:"):
        guest_id = user_id[6:]
        response.set_cookie(
            "calux_guest_id", guest_id,
            max_age=365 * 24 * 3600, path="/", httponly=True, samesite="lax",
        )
