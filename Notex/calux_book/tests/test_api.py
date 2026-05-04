"""Integration tests for Calux Book API endpoints using httpx AsyncClient."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

os.environ["JWT_SECRET"] = "test-api-secret"
os.environ["OPENAI_API_KEY"] = ""
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
os.environ["LOG_LEVEL"] = "WARNING"


@pytest_asyncio.fixture
async def client(tmp_path):
    """Create a test async client for the FastAPI app."""
    from httpx import ASGITransport, AsyncClient

    from calux_book.config import Settings

    tmp = str(tmp_path)
    cfg = Settings(
        openai_api_key="",
        ollama_base_url="http://localhost:11434",
        jwt_secret="test-api-secret",
        store_path=os.path.join(tmp, "test.db"),
        log_level="WARNING",
        # New architecture settings
        lancedb_path=os.path.join(tmp, "test_lancedb"),
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        embedding_threads=1,
        embedding_batch_size=16,
        sparse_embedding_model="Qdrant/bm25",
        enable_sparse_embedding=False,
        parser_default="pdfium",
        parser_complex="pdfium",
        parser_ocr_fallback="rapidocr",
        enable_ocr_fallback=False,
        enable_fast_path=True,
        hardware_tier="auto",
        # Reranker off in tests
        enable_reranking=False,
        reranker_model="Xenova/ms-marco-MiniLM-L-6-v2",
        rerank_candidates=20,
    )

    # Patch embedding + parser + hardware singletons so API tests don't need real models
    import calux_book.embedding as emb_mod
    import calux_book.parser_router as pr_mod
    import calux_book.hardware as hw_mod
    from tests.conftest import _make_mock_embedding_engine, _make_mock_hardware_profile

    mock_engine = _make_mock_embedding_engine()
    mock_hw = _make_mock_hardware_profile(tier="cpu")
    original_get_emb = emb_mod.get_embedding_engine
    original_get_pr = pr_mod.get_parser_router
    original_hw_profile = hw_mod._profile
    emb_mod._engine_instance = None
    pr_mod._router_instance = None
    hw_mod._profile = mock_hw
    emb_mod.get_embedding_engine = lambda **kw: mock_engine
    pr_mod.get_parser_router = lambda **kw: pr_mod.ParserRouter(enable_ocr_fallback=False)

    try:
        from calux_book.server import create_app

        app = create_app(cfg)

        # Manually trigger startup events (httpx ASGI transport does not fire lifespan)
        for handler in app.router.on_startup:
            await handler()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

        # Manually trigger shutdown events
        for handler in app.router.on_shutdown:
            await handler()
    finally:
        emb_mod.get_embedding_engine = original_get_emb
        emb_mod._engine_instance = None
        pr_mod.get_parser_router = original_get_pr
        pr_mod._router_instance = None
        hw_mod._profile = original_hw_profile


def _auth_headers(user_id: str = "test-user") -> dict[str, str]:
    from calux_book.auth import generate_jwt

    token = generate_jwt(user_id, "test-api-secret")
    return {"Authorization": f"Bearer {token}"}


class TestHealthEndpoint:
    async def test_health(self, client):
        resp = await client.get("/api/health", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "1.0.0"


class TestNotebookEndpoints:
    async def test_create_notebook(self, client):
        resp = await client.post(
            "/api/notebooks",
            json={"name": "Test NB", "description": "A test"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test NB"
        assert data["id"]

    async def test_list_notebooks(self, client):
        hdrs = _auth_headers("list-user")
        await client.post("/api/notebooks", json={"name": "NB1"}, headers=hdrs)
        await client.post("/api/notebooks", json={"name": "NB2"}, headers=hdrs)

        resp = await client.get("/api/notebooks", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    async def test_get_notebook(self, client):
        hdrs = _auth_headers()
        resp = await client.post("/api/notebooks", json={"name": "Get Me"}, headers=hdrs)
        nb_id = resp.json()["id"]

        resp = await client.get(f"/api/notebooks/{nb_id}", headers=hdrs)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Get Me"

    async def test_update_notebook(self, client):
        hdrs = _auth_headers()
        resp = await client.post("/api/notebooks", json={"name": "Old"}, headers=hdrs)
        nb_id = resp.json()["id"]

        resp = await client.put(
            f"/api/notebooks/{nb_id}",
            json={"name": "New", "description": "Updated"},
            headers=hdrs,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    async def test_delete_notebook(self, client):
        hdrs = _auth_headers()
        resp = await client.post("/api/notebooks", json={"name": "Del"}, headers=hdrs)
        nb_id = resp.json()["id"]

        resp = await client.delete(f"/api/notebooks/{nb_id}", headers=hdrs)
        assert resp.status_code == 204

        resp = await client.get(f"/api/notebooks/{nb_id}", headers=hdrs)
        assert resp.status_code == 404

    async def test_create_notebook_missing_name(self, client):
        resp = await client.post(
            "/api/notebooks", json={}, headers=_auth_headers(),
        )
        assert resp.status_code == 400

    async def test_notebook_stats(self, client):
        hdrs = _auth_headers("stats-user")
        resp = await client.get("/api/notebooks/stats", headers=hdrs)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestSourceEndpoints:
    async def test_add_text_source(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()

        resp = await client.post(
            f"/api/notebooks/{nb['id']}/sources",
            json={"name": "notes.txt", "type": "text", "content": "Some content here."},
            headers=hdrs,
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "notes.txt"

    async def test_list_sources(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        await client.post(
            f"/api/notebooks/{nb['id']}/sources",
            json={"name": "s1.txt", "type": "text", "content": "c1"},
            headers=hdrs,
        )

        resp = await client.get(f"/api/notebooks/{nb['id']}/sources", headers=hdrs)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_delete_source(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        src = (await client.post(
            f"/api/notebooks/{nb['id']}/sources",
            json={"name": "del.txt", "type": "text", "content": "c"},
            headers=hdrs,
        )).json()

        resp = await client.delete(
            f"/api/notebooks/{nb['id']}/sources/{src['id']}", headers=hdrs,
        )
        assert resp.status_code == 204

    async def test_guest_create_then_upload_file(self, client):
        nb_resp = await client.post("/api/notebooks", json={"name": "Guest NB"})
        assert nb_resp.status_code == 201
        nb_id = nb_resp.json()["id"]

        upload_resp = await client.post(
            "/api/upload",
            data={"notebook_id": nb_id},
            files={"file": ("guest.txt", b"guest upload content", "text/plain")},
        )
        assert upload_resp.status_code == 201
        data = upload_resp.json()
        assert data["name"] == "guest.txt"

    async def test_guest_upload_after_cookie_reset(self, client):
        nb_resp = await client.post("/api/notebooks", json={"name": "Legacy Guest NB"})
        assert nb_resp.status_code == 201
        nb_id = nb_resp.json()["id"]

        # Simulate stale browser state where notebook is known but guest cookie is lost.
        client.cookies.clear()

        upload_resp = await client.post(
            "/api/upload",
            data={"notebook_id": nb_id},
            files={"file": ("legacy-guest.txt", b"legacy guest upload", "text/plain")},
        )
        assert upload_resp.status_code == 201
        data = upload_resp.json()
        assert data["name"] == "legacy-guest.txt"


class TestNoteEndpoints:
    async def test_create_note(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()

        resp = await client.post(
            f"/api/notebooks/{nb['id']}/notes",
            json={"title": "My Note", "content": "Note body", "type": "custom"},
            headers=hdrs,
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "My Note"

    async def test_list_notes(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        await client.post(
            f"/api/notebooks/{nb['id']}/notes",
            json={"title": "N1", "content": "c", "type": "summary"},
            headers=hdrs,
        )

        resp = await client.get(f"/api/notebooks/{nb['id']}/notes", headers=hdrs)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_delete_note(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        note = (await client.post(
            f"/api/notebooks/{nb['id']}/notes",
            json={"title": "Del", "content": "c", "type": "summary"},
            headers=hdrs,
        )).json()

        resp = await client.delete(
            f"/api/notebooks/{nb['id']}/notes/{note['id']}", headers=hdrs,
        )
        assert resp.status_code == 204


class TestChatEndpoints:
    async def test_create_session(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()

        resp = await client.post(
            f"/api/notebooks/{nb['id']}/chat/sessions",
            json={"title": "Test Chat"},
            headers=hdrs,
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "Test Chat"

    async def test_list_sessions(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        await client.post(
            f"/api/notebooks/{nb['id']}/chat/sessions",
            json={"title": "S1"}, headers=hdrs,
        )

        resp = await client.get(f"/api/notebooks/{nb['id']}/chat/sessions", headers=hdrs)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_delete_session(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "NB"}, headers=hdrs)).json()
        session = (await client.post(
            f"/api/notebooks/{nb['id']}/chat/sessions",
            json={"title": "Del"}, headers=hdrs,
        )).json()

        resp = await client.delete(
            f"/api/notebooks/{nb['id']}/chat/sessions/{session['id']}", headers=hdrs,
        )
        assert resp.status_code == 204


class TestPublicEndpoints:
    async def test_list_public_notebooks(self, client):
        resp = await client.get("/public/notebooks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_public_notebook(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "Pub"}, headers=hdrs)).json()
        resp = await client.put(
            f"/api/notebooks/{nb['id']}/public",
            json={"is_public": True},
            headers=hdrs,
        )
        token = resp.json()["public_token"]

        resp = await client.get(f"/public/notebooks/{token}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Pub"

    async def test_public_notebook_sources_and_notes(self, client):
        hdrs = _auth_headers()
        nb = (await client.post("/api/notebooks", json={"name": "Pub2"}, headers=hdrs)).json()
        await client.post(
            f"/api/notebooks/{nb['id']}/sources",
            json={"name": "s.txt", "type": "text", "content": "data"},
            headers=hdrs,
        )
        resp = await client.put(
            f"/api/notebooks/{nb['id']}/public",
            json={"is_public": True},
            headers=hdrs,
        )
        token = resp.json()["public_token"]

        resp = await client.get(f"/public/notebooks/{token}/sources")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        resp = await client.get(f"/public/notebooks/{token}/notes")
        assert resp.status_code == 200


class TestFrontend:
    async def test_index_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Calux Book" in resp.text
