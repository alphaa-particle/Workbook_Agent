"""Shared pytest fixtures for Calux Book test suite."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

# Ensure tests never use real API keys — use direct assignment so that
# pydantic-settings picks up these values regardless of the host env.
os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY") or ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["JWT_SECRET"] = "test-secret-key-for-unit-tests"
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
os.environ["STORE_PATH"] = ":memory:"


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """Return a Settings instance for testing.

    Uses monkeypatch to ensure env vars are correctly set for each test,
    regardless of module-level env mutations in other test files.
    """
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-unit-tests")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("STORE_PATH", ":memory:")

    from calux_book.config import Settings

    return Settings(
        openai_api_key="",
        ollama_base_url="http://localhost:11434",
        jwt_secret="test-secret-key-for-unit-tests",
        store_path=":memory:",
        chunk_size=50,
        chunk_overlap=10,
        max_sources=3,
        max_context_length=2000,
        log_level="WARNING",
        # New architecture settings
        lancedb_path=str(tmp_path / "test_lancedb"),
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        embedding_threads=1,
        embedding_batch_size=16,
        sparse_embedding_model="Qdrant/bm25",
        enable_sparse_embedding=False,   # off in tests for speed
        parser_default="pdfium",
        parser_complex="pdfium",
        parser_ocr_fallback="rapidocr",
        enable_ocr_fallback=False,
        enable_fast_path=True,
        hardware_tier="auto",
        # Reranker off in tests to avoid loading real model
        enable_reranking=False,
        reranker_model="Xenova/ms-marco-MiniLM-L-6-v2",
        rerank_candidates=20,
    )


@pytest_asyncio.fixture
async def store(tmp_dir: Path):
    """Return an initialized Store with a temporary SQLite database."""
    from calux_book.store import Store

    db_path = str(tmp_dir / "test.db")
    s = Store(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def store_memory():
    """Return an initialized Store using in-memory SQLite."""
    from calux_book.store import Store

    s = Store(":memory:")
    await s.initialize()
    yield s
    await s.close()


def _make_mock_embedding_engine():
    """Create a mock EmbeddingEngine that returns deterministic vectors."""
    import hashlib

    class MockEmbeddingEngine:
        def __init__(self):
            self.dense_model_name = "mock-model"
            self.enable_sparse = False
            self._dense = True  # mark as "loaded"
            self._dim = 8

        @property
        def dimension(self):
            return self._dim

        def _text_to_vec(self, text: str) -> list[float]:
            """Deterministic mini-vector from text hash."""
            h = hashlib.md5(text.encode()).hexdigest()
            return [int(h[i:i+2], 16) / 255.0 for i in range(0, 16, 2)]

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [self._text_to_vec(t) for t in texts]

        def embed_query(self, query: str) -> list[float]:
            return self._text_to_vec(query)

        def sparse_embed_texts(self, texts):
            return []

        def sparse_embed_query(self, query):
            return None

    return MockEmbeddingEngine()


def _make_mock_hardware_profile(*, tier: str = "cpu") -> "HardwareProfile":
    """Create a mock HardwareProfile for testing."""
    from calux_book.hardware import HardwareProfile

    return HardwareProfile(
        has_cuda=(tier == "gpu"),
        gpu_name="Mock GPU" if tier == "gpu" else "",
        vram_mb=8192 if tier == "gpu" else 0,
        ram_mb=16384,
        cpu_cores=4,
        tier=tier,
    )


@pytest.fixture
def vector_store(settings, tmp_path):
    """Return a VectorStore instance for testing with mocked embeddings."""
    import calux_book.embedding as emb_mod
    import calux_book.parser_router as pr_mod
    import calux_book.hardware as hw_mod

    # Use a unique LanceDB path per test
    settings.lancedb_path = str(tmp_path / "vs_lancedb")

    mock_engine = _make_mock_embedding_engine()
    mock_hw = _make_mock_hardware_profile(tier="cpu")

    # Patch the singleton getters so VectorStore picks up mocks
    original_get_emb = emb_mod.get_embedding_engine
    original_get_pr = pr_mod.get_parser_router
    original_get_hw = hw_mod.get_hardware_profile
    original_hw_profile = hw_mod._profile

    emb_mod._engine_instance = None
    pr_mod._router_instance = None
    hw_mod._profile = mock_hw

    def _mock_get_emb(**kwargs):
        return mock_engine

    def _mock_get_pr(**kwargs):
        from calux_book.parser_router import ParserRouter
        return ParserRouter(enable_ocr_fallback=False)

    emb_mod.get_embedding_engine = _mock_get_emb
    pr_mod.get_parser_router = _mock_get_pr

    try:
        from calux_book.vector_store import VectorStore
        vs = VectorStore(settings)
        yield vs
    finally:
        emb_mod.get_embedding_engine = original_get_emb
        emb_mod._engine_instance = None
        pr_mod.get_parser_router = original_get_pr
        pr_mod._router_instance = None
        hw_mod._profile = original_hw_profile
