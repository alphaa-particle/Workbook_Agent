"""Tests for calux_book.auth and calux_book.middleware."""

from __future__ import annotations

import time

import pytest

from calux_book.auth import decode_jwt, generate_jwt, get_origin_from_url


class TestJWT:
    def test_generate_and_decode(self):
        secret = "test-secret"
        token = generate_jwt("user123", secret, expires_days=1)
        assert isinstance(token, str)
        assert len(token) > 20

        claims = decode_jwt(token, secret)
        assert claims is not None
        assert claims["user_id"] == "user123"

    def test_invalid_token(self):
        claims = decode_jwt("invalid.token.here", "secret")
        assert claims is None

    def test_wrong_secret(self):
        token = generate_jwt("user1", "secret-a")
        claims = decode_jwt(token, "secret-b")
        assert claims is None

    def test_expired_token(self):
        # Generate a token that expires immediately
        from jose import jwt as jose_jwt

        claims = {
            "user_id": "expired",
            "exp": int(time.time()) - 10,
        }
        token = jose_jwt.encode(claims, "secret", algorithm="HS256")
        result = decode_jwt(token, "secret")
        assert result is None  # should be expired


class TestGetOriginFromUrl:
    def test_full_url(self):
        assert get_origin_from_url("https://example.com/callback") == "https://example.com"

    def test_with_port(self):
        assert get_origin_from_url("http://localhost:8080/auth") == "http://localhost:8080"

    def test_empty(self):
        assert get_origin_from_url("") == ""

    def test_invalid(self):
        result = get_origin_from_url("not-a-url")
        assert result == ""


class TestMiddlewareHelpers:
    """Test middleware utility functions (without a full ASGI stack)."""

    def test_extract_token_from_header(self):
        from calux_book.middleware import _extract_token
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"authorization": "Bearer test-token-abc"}
        request.cookies = {}
        request.query_params = {}
        assert _extract_token(request) == "test-token-abc"

    def test_extract_token_from_cookie(self):
        from calux_book.middleware import _extract_token
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.cookies = {"token": "cookie-token"}
        request.query_params = {}
        assert _extract_token(request) == "cookie-token"

    def test_extract_token_from_query(self):
        from calux_book.middleware import _extract_token
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.cookies = {}
        request.query_params = {"token": "query-token"}
        assert _extract_token(request) == "query-token"

    def test_get_client_ip_forwarded(self):
        from calux_book.middleware import get_client_ip
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        request.client = MagicMock(host="127.0.0.1")
        assert get_client_ip(request) == "1.2.3.4"

    def test_get_client_ip_direct(self):
        from calux_book.middleware import get_client_ip
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock(host="192.168.1.1")
        assert get_client_ip(request) == "192.168.1.1"

    def test_extract_user_id_with_jwt(self):
        from calux_book.middleware import extract_user_id
        from unittest.mock import MagicMock

        secret = "test-secret"
        token = generate_jwt("authed-user", secret)
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {token}"}
        request.cookies = {}
        request.query_params = {}

        user_id = extract_user_id(request, secret)
        assert user_id == "authed-user"

    def test_extract_user_id_guest_fallback(self):
        from calux_book.middleware import extract_user_id
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.cookies = {}
        request.query_params = {}

        user_id = extract_user_id(request, "secret")
        assert user_id.startswith("guest:")

    def test_extract_user_id_optional_empty(self):
        from calux_book.middleware import extract_user_id_optional
        from unittest.mock import MagicMock

        request = MagicMock()
        request.headers = {}
        request.cookies = {}
        request.query_params = {}

        result = extract_user_id_optional(request, "secret")
        assert result == ""
