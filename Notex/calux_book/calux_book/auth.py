"""Authentication — OAuth handlers and JWT utilities for Calux Book."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from jose import jwt as jose_jwt

from .config import Settings
from .models import ActivityLog, User
from .store import Store

logger = logging.getLogger("calux_book.auth")


def generate_jwt(user_id: str, secret: str, expires_days: int = 7) -> str:
    """Generate a signed JWT token."""
    claims = {
        "user_id": user_id,
        "exp": int(time.time()) + expires_days * 86400,
    }
    return jose_jwt.encode(claims, secret, algorithm="HS256")


def decode_jwt(token: str, secret: str) -> dict[str, Any] | None:
    """Decode and validate a JWT token. Returns claims or None."""
    try:
        return jose_jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def get_origin_from_url(url: str) -> str:
    """Extract ``scheme://host`` from a URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class AuthHandler:
    """Handles GitHub / Google OAuth flows and JWT issuance."""

    def __init__(self, cfg: Settings, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # -- GitHub OAuth ---------------------------------------------------------

    async def github_auth_url(self) -> str:
        if not self.cfg.github_client_id:
            raise RuntimeError("GitHub OAuth not configured")
        return (
            f"https://github.com/login/oauth/authorize?"
            f"client_id={self.cfg.github_client_id}"
            f"&redirect_uri={self.cfg.github_redirect_url}"
            f"&scope=user:email read:user"
            f"&state=state"
        )

    async def github_callback(self, code: str) -> tuple[str, User]:
        """Exchange code for token, fetch user info, create user, return (jwt, user)."""
        async with httpx.AsyncClient(timeout=30) as client:
            # Exchange code for token
            resp = await client.post(
                "https://github.com/login/oauth/access_token",
                json={
                    "client_id": self.cfg.github_client_id,
                    "client_secret": self.cfg.github_client_secret,
                    "code": code,
                    "redirect_uri": self.cfg.github_redirect_url,
                },
                headers={"Accept": "application/json"},
            )
            token_data = resp.json()
            access_token = token_data.get("access_token", "")

            # Fetch user info
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            gh_user = resp.json()
            email = gh_user.get("email", "")
            name = gh_user.get("name") or gh_user.get("login", "")
            avatar = gh_user.get("avatar_url", "")

            # Fetch primary email if not public
            if not email:
                resp = await client.get(
                    "https://api.github.com/user/emails",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                emails = resp.json()
                for e in emails:
                    if e.get("primary") and e.get("verified"):
                        email = e["email"]
                        break
            if not email:
                email = f"{gh_user.get('login', 'unknown')}@github.com"

        user = User(email=email, name=name, avatar_url=avatar, provider="github")
        await self.store.create_user(user)
        db_user = await self.store.get_user_by_email(email)
        if not db_user:
            raise RuntimeError("Failed to create user")

        jwt_token = generate_jwt(db_user.id, self.cfg.jwt_secret)
        return jwt_token, db_user

    # -- Google OAuth ---------------------------------------------------------

    async def google_auth_url(self) -> str:
        if not self.cfg.google_client_id:
            raise RuntimeError("Google OAuth not configured")
        return (
            f"https://accounts.google.com/o/oauth2/v2/auth?"
            f"client_id={self.cfg.google_client_id}"
            f"&redirect_uri={self.cfg.google_redirect_url}"
            f"&response_type=code"
            f"&scope=openid email profile"
            f"&state=state"
            f"&access_type=online"
        )

    async def google_callback(self, code: str) -> tuple[str, User]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.cfg.google_client_id,
                    "client_secret": self.cfg.google_client_secret,
                    "code": code,
                    "redirect_uri": self.cfg.google_redirect_url,
                    "grant_type": "authorization_code",
                },
            )
            token_data = resp.json()
            access_token = token_data.get("access_token", "")

            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            g_user = resp.json()
            email = g_user.get("email", "")
            name = g_user.get("name", "")
            avatar = g_user.get("picture", "")

        user = User(email=email, name=name, avatar_url=avatar, provider="google")
        await self.store.create_user(user)
        db_user = await self.store.get_user_by_email(email)
        if not db_user:
            raise RuntimeError("Failed to create user")

        jwt_token = generate_jwt(db_user.id, self.cfg.jwt_secret)
        return jwt_token, db_user
