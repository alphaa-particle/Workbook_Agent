"""Configuration management for Calux Book.

Loads settings from environment variables and .env files with sensible defaults.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# .env loading — try .env then .env.local for local overrides
# ---------------------------------------------------------------------------
load_dotenv(dotenv_path=".env", override=False)
load_dotenv(dotenv_path=".env.local", override=True)


class Settings(BaseSettings):
    """Application settings sourced from environment."""

    # -- Server ---------------------------------------------------------------
    server_host: str = Field(default="0.0.0.0", alias="SERVER_HOST")
    server_port: int = Field(default=8080, alias="SERVER_PORT")

    # -- LLM ------------------------------------------------------------------
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="gemma3:4b", alias="OLLAMA_MODEL")

    # -- Image generation -----------------------------------------------------
    image_provider: str = Field(default="gemini", alias="IMAGE_PROVIDER")
    glm_api_key: str = Field(default="", alias="GLM_API_KEY")
    glm_image_model: str = Field(default="glm-image", alias="GLM_IMAGE_MODEL")
    gemini_image_model: str = Field(default="gemini-2.0-flash-exp", alias="GEMINI_IMAGE_MODEL")
    zimage_api_key: str = Field(default="", alias="ZIMAGE_API_KEY")
    zimage_model: str = Field(default="z-image-turbo", alias="ZIMAGE_MODEL")

    # -- Embedding (fastembed on CPU) -----------------------------------------
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5", alias="EMBEDDING_MODEL",
    )
    embedding_dim: int = Field(default=384, alias="EMBEDDING_DIM")
    embedding_threads: int = Field(
        default=2, alias="EMBEDDING_THREADS",
        description="ONNX thread count for fastembed — keep low on laptops",
    )
    embedding_batch_size: int = Field(
        default=16, alias="EMBEDDING_BATCH_SIZE",
        description="Batch size for embedding — auto-tuned by hardware profile",
    )
    sparse_embedding_model: str = Field(
        default="Qdrant/bm25", alias="SPARSE_EMBEDDING_MODEL",
    )
    enable_sparse_embedding: bool = Field(
        default=True, alias="ENABLE_SPARSE_EMBEDDING",
    )

    # -- Reranker (fastembed cross-encoder on CPU) ----------------------------
    reranker_model: str = Field(
        default="Xenova/ms-marco-MiniLM-L-6-v2",
        alias="RERANKER_MODEL",
        description="Cross-encoder model for reranking (~22 MB ONNX, auto-downloads)",
    )
    enable_reranking: bool = Field(
        default=True, alias="ENABLE_RERANKING",
    )
    rerank_candidates: int = Field(
        default=20, alias="RERANK_CANDIDATES",
        description="Number of RRF candidates to rerank via cross-encoder",
    )

    # -- Vector store (LanceDB) -----------------------------------------------
    lancedb_path: str = Field(default="./data/lancedb", alias="LANCEDB_PATH")

    # -- Persistence store (checkpoints) --------------------------------------
    store_type: str = Field(default="sqlite", alias="STORE_TYPE")
    store_path: str = Field(default="./data/checkpoints.db", alias="STORE_PATH")

    # -- Agent ----------------------------------------------------------------
    max_sources: int = Field(default=8, alias="MAX_SOURCES")
    max_context_length: int = Field(
        default=6000, alias="MAX_CONTEXT_LENGTH",
        description="Context chars for chat prompt — keep ≤6K for qwen2.5:7b on Ollama",
    )
    chunk_size: int = Field(
        default=800, alias="CHUNK_SIZE",
        description="Target chars per sub-chunk (bge-small sweet spot ~200-400 tokens)",
    )
    chunk_overlap: int = Field(default=128, alias="CHUNK_OVERLAP")

    # -- Summarisation tuning ------------------------------------------------
    summary_concurrency: int = Field(
        default=6, alias="SUMMARY_CONCURRENCY",
        description="Max parallel LLM calls during map-reduce summarisation",
    )
    summary_max_batches: int = Field(
        default=40, alias="SUMMARY_MAX_BATCHES",
        description="Cap batches in map-reduce; merge chunks if exceeded",
    )
    summary_batch_fill: float = Field(
        default=0.80, alias="SUMMARY_BATCH_FILL",
        description="Fraction of context window to use per summary batch",
    )
    summary_group_size: int = Field(
        default=5, alias="SUMMARY_GROUP_SIZE",
        description="Number of batch-summaries to merge per group in hierarchical reduce",
    )
    summary_timeout: int = Field(
        default=600, alias="SUMMARY_TIMEOUT",
        description="Overall timeout in seconds for a /transform request",
    )

    # -- Parser router --------------------------------------------------------
    parser_default: str = Field(
        default="pdfium", alias="PARSER_DEFAULT",
        description="Default PDF parser: pypdfium2 text extraction",
    )
    parser_complex: str = Field(
        default="pdfium", alias="PARSER_COMPLEX",
        description="PDF parser for complex documents (same as default)",
    )
    parser_ocr_fallback: str = Field(
        default="rapidocr", alias="PARSER_OCR_FALLBACK",
    )
    enable_ocr_fallback: bool = Field(default=True, alias="ENABLE_OCR_FALLBACK")
    enable_fast_path: bool = Field(
        default=True, alias="ENABLE_FAST_PATH",
        description="Use zero-AI extractors for .docx/.csv/.md/.txt",
    )
    hardware_tier: str = Field(
        default="auto", alias="HARDWARE_TIER",
        description="Override hardware tier: 'auto', 'gpu', or 'cpu'",
    )

    # -- Guest data management ------------------------------------------------
    guest_expiry_days: int = Field(
        default=30, alias="GUEST_EXPIRY_DAYS",
        description="Auto-delete guest data older than N days (0 = disabled)",
    )

    # -- Feature flags --------------------------------------------------------
    allow_multiple_notes_of_same_type: bool = Field(default=True, alias="ALLOW_MULTIPLE_NOTES_OF_SAME_TYPE")

    # -- Auth -----------------------------------------------------------------
    jwt_secret: str = Field(default="your-secret-key-change-me", alias="JWT_SECRET")

    # -- GitHub OAuth ---------------------------------------------------------
    github_client_id: str = Field(default="", alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field(default="", alias="GITHUB_CLIENT_SECRET")
    github_redirect_url: str = Field(default="", alias="GITHUB_REDIRECT_URL")

    # -- Google OAuth ---------------------------------------------------------
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_url: str = Field(default="", alias="GOOGLE_REDIRECT_URL")

    # -- Logging ---------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        populate_by_name=True,
        env_prefix="",
        extra="ignore",
    )

    # -- Derived helpers ------------------------------------------------------
    @model_validator(mode="after")
    def _resolve_provider_urls(self) -> "Settings":
        """Auto-detect Ollama vs OpenAI base URL."""
        if not self.openai_base_url and self.ollama_base_url and not self.openai_api_key:
            self.openai_base_url = self.ollama_base_url
        if not self.openai_base_url and self.openai_model:
            if "ollama" in self.openai_model or "llama" in self.openai_model:
                self.openai_base_url = self.ollama_base_url
        return self

    @property
    def is_ollama(self) -> bool:
        if self.ollama_base_url and not self.openai_api_key:
            return True
        return "11434" in (self.openai_base_url or "")

    @property
    def base_url(self) -> str:
        return self.openai_base_url or self.ollama_base_url or ""

    def get_image_model(self) -> str:
        mapping = {
            "glm": self.glm_image_model,
            "zimage": self.zimage_model,
            "gemini": self.gemini_image_model,
        }
        return mapping.get(self.image_provider, self.gemini_image_model)


def validate_settings(cfg: Settings) -> None:
    """Raise ``ValueError`` if the configuration is invalid."""
    has_openai = bool(cfg.openai_api_key)
    has_ollama = bool(cfg.ollama_base_url) or ("11434" in (cfg.openai_base_url or ""))
    if not has_openai and not has_ollama:
        raise ValueError("Either OPENAI_API_KEY or OLLAMA_BASE_URL must be set")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()  # type: ignore[call-arg]
