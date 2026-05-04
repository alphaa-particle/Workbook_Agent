"""Embedding engine for Calux Book — fastembed on CPU.

Provides dense embeddings (and optionally sparse/BM25 embeddings) using
the fastembed library, running entirely on CPU with no GPU requirement.

Dense model default : BAAI/bge-small-en-v1.5  (384-dim, ~33 MB)
Sparse model default: Qdrant/bm25             (Tantivy-backed BM25)

Thread count is capped (default 2) to keep the system responsive on
low-to-mid-end laptops.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("calux_book.embedding")


class EmbeddingEngine:
    """Wraps fastembed for dense (and optionally sparse) embeddings."""

    def __init__(
        self,
        dense_model: str = "BAAI/bge-small-en-v1.5",
        sparse_model: str = "Qdrant/bm25",
        enable_sparse: bool = True,
        threads: int = 2,
    ) -> None:
        self.dense_model_name = dense_model
        self.sparse_model_name = sparse_model
        self.enable_sparse = enable_sparse
        self.threads = max(1, threads)

        self._dense: Any = None
        self._sparse: Any = None
        self._dim: int | None = None

    # ------------------------------------------------------------------
    # Lazy initialisation
    # ------------------------------------------------------------------

    def _ensure_dense(self) -> None:
        if self._dense is not None:
            return
        from fastembed import TextEmbedding
        self._dense = TextEmbedding(
            model_name=self.dense_model_name,
            threads=self.threads,
        )
        logger.info(
            "Dense embedding model loaded: %s (threads=%d)",
            self.dense_model_name, self.threads,
        )

    def _ensure_sparse(self) -> None:
        if not self.enable_sparse:
            return
        if self._sparse is not None:
            return
        try:
            from fastembed import SparseTextEmbedding
            self._sparse = SparseTextEmbedding(model_name=self.sparse_model_name)
            logger.info("Sparse embedding model loaded: %s", self.sparse_model_name)
        except Exception as e:
            logger.warning("Sparse embeddings disabled: %s", e)
            self.enable_sparse = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Return the dense embedding dimensionality."""
        if self._dim is not None:
            return self._dim
        self._ensure_dense()
        # Probe with a single string
        vec = list(self._dense.embed(["probe"]))[0]
        self._dim = len(vec)
        return self._dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return dense embeddings for a batch of texts."""
        if not texts:
            return []
        self._ensure_dense()
        embeddings = list(self._dense.embed(texts))
        return [emb.tolist() if isinstance(emb, np.ndarray) else list(emb) for emb in embeddings]

    def embed_query(self, query: str) -> list[float]:
        """Return the dense embedding for a single query string."""
        self._ensure_dense()
        vecs = list(self._dense.query_embed(query))
        if not vecs:
            vecs = list(self._dense.embed([query]))
        emb = vecs[0]
        return emb.tolist() if isinstance(emb, np.ndarray) else list(emb)

    def sparse_embed_texts(self, texts: list[str]) -> list[dict[str, Any]]:
        """Return sparse embeddings as list of {indices, values} dicts.

        Returns an empty list when sparse embeddings are disabled.
        """
        if not self.enable_sparse or not texts:
            return []
        self._ensure_sparse()
        if self._sparse is None:
            return []
        results: list[dict[str, Any]] = []
        for emb in self._sparse.embed(texts):
            results.append({
                "indices": emb.indices.tolist(),
                "values": emb.values.tolist(),
            })
        return results

    def sparse_embed_query(self, query: str) -> dict[str, Any] | None:
        """Return sparse embedding for a single query, or None."""
        if not self.enable_sparse:
            return None
        self._ensure_sparse()
        if self._sparse is None:
            return None
        for emb in self._sparse.query_embed(query):
            return {
                "indices": emb.indices.tolist(),
                "values": emb.values.tolist(),
            }
        return None


# ---------------------------------------------------------------------------
# Cross-encoder reranker (fastembed ONNX, ~22 MB)
# ---------------------------------------------------------------------------

class Reranker:
    """Wraps fastembed TextCrossEncoder for cross-encoder reranking.

    Model default: Xenova/ms-marco-MiniLM-L-6-v2 (Apache-2.0, ~22 MB ONNX).
    Lazy-loaded on first use, runs on CPU via ONNX Runtime.
    """

    def __init__(
        self,
        model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2",
        threads: int = 2,
    ) -> None:
        self.model_name = model_name
        self.threads = max(1, threads)
        self._model: Any = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from fastembed import TextCrossEncoder
            self._model = TextCrossEncoder(
                model_name=self.model_name,
                # threads=self.threads,  # not all versions support this
            )
            logger.info(
                "Cross-encoder reranker loaded: %s", self.model_name,
            )
        except Exception as e:
            logger.warning("Reranker unavailable: %s", e)
            self._model = None

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 10,
    ) -> list[tuple[int, float]]:
        """Rerank documents by relevance to query.

        Returns list of (original_index, score) sorted by score descending.
        """
        if not documents:
            return []
        self._ensure_model()
        if self._model is None:
            # Fallback: return original order with dummy scores
            return [(i, 1.0 / (i + 1)) for i in range(min(top_k, len(documents)))]

        try:
            pairs = [(query, doc) for doc in documents]
            scores = list(self._model.rerank(query, documents))
            # fastembed rerank returns RerankResult objects with .score and .index
            indexed_scores: list[tuple[int, float]] = []
            for result in scores:
                idx = getattr(result, "index", None)
                score = getattr(result, "relevance_score",
                         getattr(result, "score", 0.0))
                if idx is not None:
                    indexed_scores.append((int(idx), float(score)))

            if not indexed_scores:
                # Fallback if rerank result format is unexpected
                raw_scores = list(self._model.predict(pairs))
                indexed_scores = [
                    (i, float(s)) for i, s in enumerate(raw_scores)
                ]

            indexed_scores.sort(key=lambda x: x[1], reverse=True)
            return indexed_scores[:top_k]
        except Exception as e:
            logger.warning("Reranking failed, using original order: %s", e)
            return [(i, 1.0 / (i + 1)) for i in range(min(top_k, len(documents)))]


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_engine_instance: EmbeddingEngine | None = None


def get_embedding_engine(
    dense_model: str = "BAAI/bge-small-en-v1.5",
    sparse_model: str = "Qdrant/bm25",
    enable_sparse: bool = True,
    threads: int = 2,
) -> EmbeddingEngine:
    """Return a lazily-created singleton EmbeddingEngine."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = EmbeddingEngine(
            dense_model=dense_model,
            sparse_model=sparse_model,
            enable_sparse=enable_sparse,
            threads=threads,
        )
    return _engine_instance


_reranker_instance: Reranker | None = None


def get_reranker(
    model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2",
    threads: int = 2,
) -> Reranker:
    """Return a lazily-created singleton Reranker."""
    global _reranker_instance
    if _reranker_instance is None:
        _reranker_instance = Reranker(
            model_name=model_name,
            threads=threads,
        )
    return _reranker_instance
