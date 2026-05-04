"""Vector store for Calux Book — LanceDB with fastembed embeddings.

Replaces the in-memory BM25+trigram store with a persistent LanceDB backend.

Features:
  - Dense vector search via fastembed embeddings
  - BM25 full-text search via LanceDB's built-in Tantivy integration
  - Reciprocal Rank Fusion (RRF) to merge dense + BM25 results
  - Persistent on-disk storage — survives restarts
  - Document extraction delegated to ParserRouter
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import lancedb

from .config import Settings
from .embedding import EmbeddingEngine, Reranker, get_embedding_engine, get_reranker
from .parser_router import ParserRouter, ProgressCallback, get_parser_router

logger = logging.getLogger("calux_book.vector")
chunk_logger = logging.getLogger("calux_book.chunking")

# ---------------------------------------------------------------------------
# Data types (kept for backward compatibility with agent / server)
# ---------------------------------------------------------------------------

@dataclass
class Document:
    """A chunk of text with metadata."""
    page_content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorStats:
    total_documents: int = 0
    total_vectors: int = 0
    dimension: int = 384


# ---------------------------------------------------------------------------
# LanceDB table name
# ---------------------------------------------------------------------------

_TABLE_NAME = "chunks"
_PAGE_FIELD_DEFAULTS: dict[str, Any] = {
    "page_number": 1,
    "page_chunk_idx": 0,
    "section_title": "",
    "section_path": "",
    "block_type": "text",
}


# ---------------------------------------------------------------------------
# Stopwords for keyword extraction (lightweight, no dependency)
# ---------------------------------------------------------------------------
_STOPWORDS: set[str] = {
    "a", "an", "the", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "under",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "why", "how", "all", "each", "every", "both", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "just", "because", "about", "up", "out",
    "if", "it", "its", "this", "that", "these", "those", "what", "which",
    "who", "whom", "he", "she", "they", "we", "you", "i", "me", "my",
    "his", "her", "our", "their", "your", "also", "page", "section",
}


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class VectorStore:
    """Persistent vector store backed by LanceDB + fastembed.

    Uses hybrid dense + BM25 search with page-wise chunking for retrieval.
    """

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._ingested: set[str] = set()

        # LanceDB connection (on-disk)
        db_path = cfg.lancedb_path
        Path(db_path).mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(db_path)
        self._table: lancedb.table.Table | None = None

        # Embedding engine (fastembed on CPU, thread-capped)
        self._embedder: EmbeddingEngine = get_embedding_engine(
            dense_model=cfg.embedding_model,
            sparse_model=cfg.sparse_embedding_model,
            enable_sparse=cfg.enable_sparse_embedding,
            threads=getattr(cfg, "embedding_threads", 2),
        )

        # Parser router (pypdfium2 for PDF + fast-path for non-PDF)
        self._parser: ParserRouter = get_parser_router(
            default_parser=cfg.parser_default,
            complex_parser=cfg.parser_complex,
            ocr_fallback=cfg.parser_ocr_fallback,
            enable_ocr_fallback=cfg.enable_ocr_fallback,
            enable_fast_path=getattr(cfg, "enable_fast_path", True),
        )

        # Cross-encoder reranker (lazy, ONNX on CPU)
        self._reranker: Reranker | None = None
        if getattr(cfg, "enable_reranking", True):
            self._reranker = get_reranker(
                model_name=getattr(cfg, "reranker_model", "Xenova/ms-marco-MiniLM-L-6-v2"),
                threads=getattr(cfg, "embedding_threads", 2),
            )

        # Ensure table exists
        self._ensure_table()

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the chunks table if it does not yet exist."""
        if _TABLE_NAME in self._db.list_tables():
            self._table = self._db.open_table(_TABLE_NAME)
            # Rebuild ingested fingerprint set — only select ingest_key
            # column to avoid materializing huge vector arrays.
            try:
                tbl = (
                    self._table.search()
                    .select(["ingest_key"])
                    .limit(500_000)
                    .to_arrow()
                )
                col = tbl.column("ingest_key")
                self._ingested = set(col.to_pylist())
            except Exception:
                # Fallback: full table scan (older LanceDB versions)
                try:
                    tbl = self._table.to_arrow()
                    col = tbl.column("ingest_key")
                    self._ingested = set(col.to_pylist())
                except Exception:
                    self._ingested = set()
            self._upgrade_table_schema_if_needed()
            logger.info(
                "Opened existing LanceDB table '%s' (%d fingerprints loaded)",
                _TABLE_NAME, len(self._ingested),
            )
        else:
            self._table = None
            logger.info("LanceDB table '%s' will be created on first ingest", _TABLE_NAME)

    def _create_table(self, records: list[dict[str, Any]]) -> None:
        """Create the LanceDB table with the first batch of records."""
        normalized = [self._normalize_record(r) for r in records]
        self._table = self._db.create_table(_TABLE_NAME, data=normalized, mode="overwrite")
        # Create a full-text search index on the text column for BM25
        try:
            self._table.create_fts_index("text", replace=True)
        except Exception as e:
            logger.warning("FTS index creation deferred: %s", e)

    def _upgrade_table_schema_if_needed(self) -> None:
        """Upgrade existing table records with page-index fields when absent."""
        if self._table is None:
            return
        try:
            schema_names = set(self._table.schema.names)
        except Exception:
            schema_names = set()

        missing = [k for k in _PAGE_FIELD_DEFAULTS if k not in schema_names]
        if not missing:
            return

        logger.info("Upgrading LanceDB schema with page fields: %s", ", ".join(missing))
        try:
            tbl = self._table.to_arrow()
            existing_records = [dict(zip(tbl.column_names, row)) for row in zip(*(tbl.column(c).to_pylist() for c in tbl.column_names))]
        except Exception:
            existing_records = []

        normalized = [self._normalize_record(rec) for rec in existing_records]
        self._table = self._db.create_table(_TABLE_NAME, data=normalized, mode="overwrite")
        try:
            self._table.create_fts_index("text", replace=True)
        except Exception:
            pass

    @staticmethod
    def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
        out = dict(record)
        for field_name, default_value in _PAGE_FIELD_DEFAULTS.items():
            value = out.get(field_name)
            if value is None or value == "":
                out[field_name] = default_value
        return out

    def _add_records_with_upgrade(self, records: list[dict[str, Any]]) -> None:
        """Add records and transparently upgrade schema if older table lacks fields."""
        if self._table is None:
            self._create_table(records)
            return

        try:
            self._table.add([self._normalize_record(r) for r in records])
            return
        except Exception as e:
            msg = str(e).lower()
            if "schema" not in msg and "column" not in msg:
                raise
            logger.warning("LanceDB add schema mismatch; rebuilding table with upgraded schema")

        tbl = self._table.to_arrow()
        existing_records = [dict(zip(tbl.column_names, row)) for row in zip(*(tbl.column(c).to_pylist() for c in tbl.column_names))]
        merged = [self._normalize_record(rec) for rec in existing_records]
        merged.extend(self._normalize_record(rec) for rec in records)
        self._table = self._db.create_table(_TABLE_NAME, data=merged, mode="overwrite")
        try:
            self._table.create_fts_index("text", replace=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_source(
        self, notebook_id: str, source_id: str, source_name: str, content: str,
        *, progress_cb: ProgressCallback | None = None,
    ) -> int:
        """Chunk *content*, embed it, and store in LanceDB. Returns chunk count."""
        fingerprint = self._fingerprint(notebook_id, source_id, source_name, content)
        if not content.strip():
            return 0

        with self._lock:
            if fingerprint in self._ingested:
                logger.info("Skipping duplicate ingest for '%s'", source_name)
                return 0

        page_chunks = self._split_into_page_chunks(
            content,
            self.cfg.chunk_size,
            self.cfg.chunk_overlap,
            source_name=source_name,
        )
        chunks = [entry["text"] for entry in page_chunks]
        if not chunks:
            return 0

        # Build contextual texts for embedding (prefix + chunk text)
        # The prefix is NOT stored in the text field but improves
        # embedding discriminability for large books with overlapping topics.
        embed_texts = []
        for entry in page_chunks:
            prefix = entry.get("context_prefix", "")
            embed_texts.append(prefix + entry["text"] if prefix else entry["text"])

        # Log chunking details
        chunk_logger.info(
            "Chunking '%s': %d pages → %d chunks (size=%d, overlap=%d)",
            source_name,
            len(set(pc.get("page_number", 1) for pc in page_chunks)),
            len(chunks),
            self.cfg.chunk_size,
            self.cfg.chunk_overlap,
        )
        for i, pc in enumerate(page_chunks):
            chunk_logger.debug(
                "  chunk[%d] page=%d section='%s' len=%d",
                i, pc.get("page_number", 1),
                pc.get("section_title", "")[:60],
                len(pc.get("text", "")),
            )

        if progress_cb:
            try:
                progress_cb("embedding", f"Embedding {len(chunks)} chunks", 10)
            except Exception:
                pass

        # Embed all chunks (using contextual prefix for better vectors)
        vectors = self._embedder.embed_texts(embed_texts)

        if progress_cb:
            try:
                progress_cb("embedding", f"Storing {len(chunks)} chunks", 60)
            except Exception:
                pass

        records: list[dict[str, Any]] = []
        for i, (chunk_info, vec) in enumerate(zip(page_chunks, vectors)):
            chunk = chunk_info["text"]

            records.append({
                "vector": vec,
                "text": chunk,
                "notebook_id": notebook_id,
                "source_id": source_id,
                "source": source_name,
                "chunk_idx": i,
                "page_number": int(chunk_info.get("page_number", 1)),
                "page_chunk_idx": int(chunk_info.get("page_chunk_idx", 0)),
                "section_title": chunk_info.get("section_title", ""),
                "section_path": chunk_info.get("section_path", ""),
                "block_type": chunk_info.get("block_type", "text"),
                "token_count": len(_tokenize(chunk)),
                "ingest_key": fingerprint,
                "keywords": chunk_info.get("keywords", ""),
            })

        with self._lock:
            if fingerprint in self._ingested:
                return 0

            self._add_records_with_upgrade(records)
            # Rebuild FTS index to include new data
            try:
                self._table.create_fts_index("text", replace=True)
            except Exception as e:
                logger.debug("FTS re-index note: %s", e)

            self._ingested.add(fingerprint)

        if progress_cb:
            try:
                progress_cb("embedding", "Indexing complete", 95)
            except Exception:
                pass

        logger.info(
            "Ingested %d chunks from '%s' into LanceDB", len(chunks), source_name,
        )
        return len(chunks)

    def ingest_text(self, notebook_id: str, source_name: str, content: str) -> int:
        return self.ingest_source(notebook_id, source_name, source_name, content)

    # ------------------------------------------------------------------
    # Search — hybrid dense + BM25 with RRF
    # ------------------------------------------------------------------

    def similarity_search(
        self,
        notebook_id: str,
        query: str,
        num_docs: int = 8,
        source_ids: list[str] | None = None,
    ) -> list[Document]:
        """Two-stage hybrid search: RRF coarse retrieval → cross-encoder reranking.

        Pipeline:
        1. Dense (ANN) vector search  → fetch_k candidates
        2. BM25 (FTS) search          → fetch_k candidates
        3. Reciprocal Rank Fusion      → merge both lists
        4. Cross-encoder reranking     → fine-grained relevance scoring
        5. Context expansion           → sibling chunks from same page/section
        6. Source diversity filtering   → balanced results
        """
        if num_docs <= 0:
            num_docs = 8
        if self._table is None:
            return []

        where_clause = self._build_where_clause(notebook_id, source_ids)

        # Fetch more candidates for reranking (6× for large-book recall)
        fetch_k = num_docs * 6
        rerank_k = getattr(self.cfg, "rerank_candidates", 20)

        # --- Stage 1: Dense (ANN) search ---
        query_vec = self._embedder.embed_query(query)
        dense_results: list[dict[str, Any]] = []
        try:
            tbl = (
                self._table.search(query_vec, query_type="vector")
                .where(where_clause, prefilter=True)
                .limit(fetch_k)
                .to_arrow()
            )
            cols = tbl.column_names
            for row_vals in zip(*(tbl.column(c).to_pylist() for c in cols)):
                dense_results.append(dict(zip(cols, row_vals)))
        except Exception as e:
            logger.debug("Dense search error: %s", e)

        # --- Stage 2: BM25 (FTS) search ---
        fts_results: list[dict[str, Any]] = []
        try:
            tbl = (
                self._table.search(query, query_type="fts")
                .where(where_clause, prefilter=True)
                .limit(fetch_k)
                .to_arrow()
            )
            cols = tbl.column_names
            for row_vals in zip(*(tbl.column(c).to_pylist() for c in cols)):
                fts_results.append(dict(zip(cols, row_vals)))
        except Exception as e:
            logger.debug("FTS search error (index may not exist yet): %s", e)

        # --- Stage 3: Reciprocal Rank Fusion (2-way merge) ---
        fused = _rrf_merge_multi(
            [dense_results, fts_results],
            weights=[0.55, 0.45],
            k=60,
        )

        # --- Stage 4: Cross-encoder reranking ---
        if self._reranker and len(fused) > 1:
            candidates = fused[:rerank_k]
            candidate_texts = [item.get("text", "") for item in candidates]
            try:
                reranked = self._reranker.rerank(
                    query, candidate_texts, top_k=min(num_docs * 2, len(candidates)),
                )
                # Rebuild fused list with reranked order
                reranked_items = [candidates[idx] for idx, _ in reranked]
                # Append any remaining items not reranked
                reranked_texts = {item.get("text", "") for item in reranked_items}
                for item in fused[rerank_k:]:
                    if item.get("text", "") not in reranked_texts:
                        reranked_items.append(item)
                fused = reranked_items
                logger.debug(
                    "Reranked %d candidates → top-%d",
                    len(candidates), len(reranked),
                )
            except Exception as e:
                logger.debug("Reranking skipped: %s", e)

        # --- Stage 5: Context expansion for large-book sources ---
        # If any source has many pages, expand results with sibling chunks
        expanded = self._expand_sibling_chunks(fused, where_clause, num_docs)

        # --- Stage 6: Convert to Documents with source diversity ---
        result: list[Document] = []
        per_source: dict[str, int] = {}
        max_per = max(2, int(num_docs * 0.50))
        seen_text: set[str] = set()

        for item in expanded:
            if len(result) >= num_docs:
                break
            text = item.get("text", "")
            if not text:
                continue
            normalized = " ".join(text.lower().split())
            if normalized in seen_text:
                continue
            seen_text.add(normalized)

            sid = item.get("source_id") or item.get("source", "")
            if sid and per_source.get(sid, 0) >= max_per:
                continue
            result.append(self._item_to_document(item))
            if sid:
                per_source[sid] = per_source.get(sid, 0) + 1

        # Fill remaining slots ignoring diversity constraint
        if len(result) < num_docs:
            for item in expanded:
                if len(result) >= num_docs:
                    break
                text = item.get("text", "")
                normalized = " ".join(text.lower().split())
                if normalized not in seen_text:
                    result.append(self._item_to_document(item))
                    seen_text.add(normalized)

        return result

    def _expand_sibling_chunks(
        self,
        fused: list[dict[str, Any]],
        where_clause: str,
        num_docs: int,
    ) -> list[dict[str, Any]]:
        """Expand top results with sibling chunks from the same page.

        For large books (URDPFI, Neufert — 200+ pages), sub-chunks from
        the same page often provide complementary context.  This fetches
        adjacent chunks sharing the same page_number + source_id.
        """
        if not fused or self._table is None:
            return fused

        # Only expand if we have sub-chunked data (page_chunk_idx > 0 exists)
        has_sub_chunks = any(
            item.get("page_chunk_idx", 0) > 0 for item in fused[:20]
        )
        if not has_sub_chunks:
            return fused

        # Collect (source_id, page_number) pairs from top results
        page_keys: set[tuple[str, int]] = set()
        for item in fused[:num_docs]:
            sid = item.get("source_id", "")
            pg = item.get("page_number", 1)
            if sid:
                page_keys.add((sid, pg))

        if not page_keys:
            return fused

        # Fetch sibling chunks
        existing_texts = {item.get("text", "") for item in fused}
        siblings: list[dict[str, Any]] = []

        for sid, pg in list(page_keys)[:5]:  # cap at 5 page lookups
            try:
                sibling_where = (
                    f"{where_clause} AND source_id = '{_escape(sid)}' "
                    f"AND page_number = {pg}"
                )
                tbl = (
                    self._table.search()
                    .where(sibling_where)
                    .limit(10)
                    .to_arrow()
                )
                cols = tbl.column_names
                for row_vals in zip(*(tbl.column(c).to_pylist() for c in cols)):
                    rec = dict(zip(cols, row_vals))
                    if rec.get("text", "") not in existing_texts:
                        siblings.append(rec)
                        existing_texts.add(rec.get("text", ""))
            except Exception:
                pass

        # Interleave: original results first, then siblings sorted by page order
        siblings.sort(key=lambda r: (
            r.get("page_number", 1), r.get("page_chunk_idx", 0),
        ))
        return fused + siblings

    @staticmethod
    def _item_to_document(item: dict[str, Any]) -> Document:
        """Convert a raw LanceDB row dict to a Document."""
        return Document(
            page_content=item.get("text", ""),
            metadata={
                "notebook_id": item.get("notebook_id", ""),
                "source_id": item.get("source_id", ""),
                "source": item.get("source", ""),
                "chunk": item.get("chunk_idx", 0),
                "page_number": item.get("page_number", 1),
                "page_chunk_idx": item.get("page_chunk_idx", 0),
                "section_title": item.get("section_title", ""),
                "section_path": item.get("section_path", ""),
                "block_type": item.get("block_type", "text"),
                "token_count": item.get("token_count", 0),
                "ingest_key": item.get("ingest_key", ""),
                "keywords": item.get("keywords", ""),
            },
        )

    @staticmethod
    def _build_where_clause(notebook_id: str, source_ids: list[str] | None) -> str:
        clause = f"notebook_id = '{_escape(notebook_id)}'"
        if not source_ids:
            return clause
        safe_ids = [sid for sid in source_ids if sid]
        if not safe_ids:
            return clause
        if len(safe_ids) == 1:
            return clause + f" AND source_id = '{_escape(safe_ids[0])}'"
        in_values = ", ".join(f"'{_escape(s)}'" for s in safe_ids)
        return clause + f" AND source_id IN ({in_values})"

    # ------------------------------------------------------------------
    # Full-content retrieval (for summaries / map-reduce)
    # ------------------------------------------------------------------

    def get_all_chunks(
        self,
        notebook_id: str,
        source_ids: list[str] | None = None,
    ) -> list[Document]:
        """Return *all* chunks for the given sources, ordered by page then chunk index.

        Unlike ``similarity_search`` this does **no** vector/FTS ranking — it
        simply returns every stored chunk so that the agent can build a
        comprehensive summary of the full document.
        """
        if self._table is None:
            return []

        where = self._build_where_clause(notebook_id, source_ids)

        try:
            tbl = (
                self._table.search()
                .where(where)
                .limit(100_000)          # effectively unlimited
                .to_arrow()
            )
        except Exception:
            # Fallback: read entire table and filter in Python
            try:
                tbl = self._table.to_arrow()
            except Exception:
                return []

        if tbl.num_rows == 0:
            return []

        cols = tbl.column_names
        rows: list[dict[str, Any]] = []
        for row_vals in zip(*(tbl.column(c).to_pylist() for c in cols)):
            rec = dict(zip(cols, row_vals))
            # Apply notebook filter in Python as safety net
            if rec.get("notebook_id") != notebook_id:
                continue
            if source_ids:
                if rec.get("source_id") not in source_ids:
                    continue
            rows.append(rec)

        # Sort by page_number → page_chunk_idx → chunk_idx for stable order
        rows.sort(key=lambda r: (
            r.get("page_number", 1),
            r.get("page_chunk_idx", 0),
            r.get("chunk_idx", 0),
        ))

        docs: list[Document] = []
        for item in rows:
            text = item.get("text", "")
            if not text or not text.strip():
                continue
            docs.append(Document(
                page_content=text,
                metadata={
                    "notebook_id": item.get("notebook_id", ""),
                    "source_id": item.get("source_id", ""),
                    "source": item.get("source", ""),
                    "chunk": item.get("chunk_idx", 0),
                    "page_number": item.get("page_number", 1),
                    "page_chunk_idx": item.get("page_chunk_idx", 0),
                    "section_title": item.get("section_title", ""),
                    "section_path": item.get("section_path", ""),
                    "block_type": item.get("block_type", "text"),
                    "token_count": item.get("token_count", 0),
                    "ingest_key": item.get("ingest_key", ""),
                    "keywords": item.get("keywords", ""),
                },
            ))

        logger.info(
            "get_all_chunks: %d chunks for notebook=%s sources=%s",
            len(docs), notebook_id, source_ids,
        )
        return docs

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete(self, notebook_id: str, source_id: str, source_name: str) -> None:
        """Remove all chunks for a given source from LanceDB."""
        if self._table is None:
            return

        with self._lock:
            try:
                if source_id:
                    condition = (
                        f"notebook_id = '{_escape(notebook_id)}' "
                        f"AND source_id = '{_escape(source_id)}'"
                    )
                else:
                    condition = (
                        f"notebook_id = '{_escape(notebook_id)}' "
                        f"AND source = '{_escape(source_name)}'"
                    )
                self._table.delete(condition)

                # Remove matching ingest keys
                keys_to_remove = set()
                for key in list(self._ingested):
                    if source_id in key or source_name in key:
                        keys_to_remove.add(key)
                self._ingested -= keys_to_remove

            except Exception as e:
                logger.error("Delete from LanceDB failed: %s", e)

    # ------------------------------------------------------------------
    # Document extraction (delegated to ParserRouter)
    # ------------------------------------------------------------------

    def extract_document(
        self, path: str, *, progress_cb: ProgressCallback | None = None,
    ) -> str:
        """Extract text from a file using the parser router."""
        return self._parser.extract(path, progress_cb=progress_cb)

    def extract_document_pages(
        self, path: str, *, progress_cb: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Extract page-aware text records from a file using the parser router."""
        return self._parser.extract_pages(path, progress_cb=progress_cb)

    def extract_from_url(self, url: str) -> str:
        """Extract text from a URL using the parser router."""
        return self._parser.extract_from_url(url)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> VectorStats:
        with self._lock:
            count = 0
            if self._table is not None:
                try:
                    count = self._table.count_rows()
                except Exception:
                    count = 0
            dim = self._embedder.dimension if self._embedder._dense else self.cfg.embedding_dim
            return VectorStats(
                total_documents=count,
                total_vectors=count,
                dimension=dim,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _split_into_page_chunks(
        cls,
        text: str,
        chunk_size: int,
        chunk_overlap: int,
        source_name: str = "",
    ) -> list[dict[str, Any]]:
        """Split content into page-wise sub-chunks with sentence-boundary splitting.

        Pipeline:
        1. Parse ``[PAGE N]`` markers → list of pages.
        2. For each page: if text ≤ *chunk_size* → one chunk.
           Otherwise split at sentence boundaries with *chunk_overlap* overlap.
        3. Track running section titles and build ``section_path``.
        4. Extract keywords per chunk (TF-based, no dependency).
        5. Each record carries: text, page_number, page_chunk_idx,
           section_title, section_path, block_type, keywords,
           context_prefix (for contextual embedding — not stored as text).
        """
        pages = cls._extract_pages_from_text(text)
        if not pages:
            return []

        # Section tracker — propagate headings across pages
        current_sections: list[str] = []  # stack of section titles
        _heading_re = re.compile(
            r"^(?:#{1,4}\s+(.+)|(\d{1,3}(?:\.\d{1,3}){0,3})\s+([A-Z].+)|([A-Z][A-Z\s]{5,}))$"
        )

        records: list[dict[str, Any]] = []
        for page in pages:
            page_number = int(page.get("page_number", 1))
            page_text = str(page.get("text", "")).strip()
            if not page_text:
                continue

            section_title = cls._infer_section_title(page_text)
            block_type = cls._infer_block_type(page_text)

            # Update section path from headings on this page
            for line in page_text.splitlines()[:30]:
                ln = line.strip()
                if not ln:
                    continue
                m = _heading_re.match(ln)
                if m:
                    heading = (m.group(1) or m.group(3) or m.group(4) or "").strip()
                    if heading:
                        # Heuristic depth: markdown # count, or numbered depth
                        depth = 0
                        if ln.startswith("#"):
                            depth = len(ln) - len(ln.lstrip("#"))
                        elif m.group(2):
                            depth = m.group(2).count(".") + 1
                        else:
                            depth = 1
                        depth = max(1, min(depth, 4))
                        # Trim stack to parent level
                        current_sections = current_sections[: depth - 1]
                        current_sections.append(heading[:120])
                        break  # One heading per page is enough

            section_path = " > ".join(current_sections) if current_sections else ""

            # Context prefix for contextual embedding (prepended before
            # embedding but NOT stored in the text field)
            ctx_prefix = ""
            if source_name:
                ctx_prefix += f"Source: {source_name}"
            if section_title:
                ctx_prefix += f" | Section: {section_title}"
            ctx_prefix += f" | Page: {page_number}"
            ctx_prefix = ctx_prefix.strip(" |") + "\n"

            # Sub-chunk if page text exceeds chunk_size
            sub_chunks = cls._split_page_at_sentences(
                page_text, chunk_size, chunk_overlap,
            )

            page_keywords = cls._extract_keywords(page_text)

            for idx, chunk_text in enumerate(sub_chunks):
                chunk_kw = cls._extract_keywords(chunk_text) if len(sub_chunks) > 1 else page_keywords
                records.append({
                    "text": chunk_text,
                    "page_number": page_number,
                    "page_chunk_idx": idx,
                    "section_title": section_title,
                    "section_path": section_path,
                    "block_type": block_type,
                    "keywords": ",".join(chunk_kw),
                    "context_prefix": ctx_prefix,
                })
        return records

    @staticmethod
    def _split_page_at_sentences(
        text: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[str]:
        """Split *text* into chunks at sentence boundaries.

        If *text* ≤ *chunk_size* → returns [text] unchanged.
        Otherwise splits at sentence-ending punctuation (. ! ? \\n\\n)
        with *chunk_overlap* character overlap between consecutive chunks.
        """
        if len(text) <= chunk_size:
            return [text]

        # Split at sentence boundaries
        sentence_re = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")
        sentences = sentence_re.split(text)
        if not sentences:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            sent_len = len(sent)

            if current_len + sent_len > chunk_size and current:
                chunks.append(" ".join(current))
                # Overlap: keep trailing sentences that fit in overlap window
                overlap_parts: list[str] = []
                overlap_len = 0
                for s in reversed(current):
                    if overlap_len + len(s) > chunk_overlap:
                        break
                    overlap_parts.insert(0, s)
                    overlap_len += len(s)
                current = overlap_parts
                current_len = overlap_len

            current.append(sent)
            current_len += sent_len

        if current:
            chunks.append(" ".join(current))

        # Safety: if we somehow got no chunks, return original
        return chunks if chunks else [text]

    @staticmethod
    def _extract_keywords(text: str, top_k: int = 8) -> list[str]:
        """Extract top-k keywords from text using simple TF scoring.

        No external dependency — uses regex tokenization and stopword filtering.
        """
        if not text:
            return []
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        freq: dict[str, int] = {}
        for w in words:
            if w not in _STOPWORDS and len(w) > 2:
                freq[w] = freq.get(w, 0) + 1
        if not freq:
            return []
        ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in ranked[:top_k]]

    @staticmethod
    def _extract_pages_from_text(text: str) -> list[dict[str, Any]]:
        if not text:
            return []

        marker = re.compile(r"(?im)^\s*\[page\s+(\d{1,5})\]\s*$")
        lines = text.replace("\r\n", "\n").split("\n")
        pages: list[dict[str, Any]] = []
        current: list[str] = []
        current_page = 1
        seen_marker = False

        for line in lines:
            match = marker.match(line.strip())
            if match:
                seen_marker = True
                body = "\n".join(current).strip()
                if body:
                    pages.append({"page_number": current_page, "text": body})
                current_page = int(match.group(1))
                current = []
                continue
            current.append(line)

        tail = "\n".join(current).strip()
        if tail:
            pages.append({"page_number": current_page, "text": tail})

        if not pages:
            return [{"page_number": 1, "text": text}]
        if not seen_marker:
            pages[0]["page_number"] = 1
        return pages

    @staticmethod
    def _infer_section_title(text: str) -> str:
        for line in text.splitlines()[:20]:
            ln = line.strip()
            if not ln:
                continue
            if ln.startswith("#"):
                return ln.lstrip("# ").strip()[:160]
            if len(ln) <= 120 and ln == ln.upper() and any(ch.isalpha() for ch in ln):
                return ln[:160]
        return ""

    @staticmethod
    def _infer_block_type(text: str) -> str:
        if "|" in text and "---" in text:
            return "table"
        return "text"

    @staticmethod
    def _fingerprint(notebook_id: str, source_id: str, source_name: str, content: str) -> str:
        raw = f"{notebook_id}\n{source_id}\n{source_name}\n{content}"
        return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def _rrf_merge_multi(
    result_lists: list[list[dict[str, Any]]],
    weights: list[float] | None = None,
    k: int = 60,
) -> list[dict[str, Any]]:
    """Merge N ranked lists using weighted Reciprocal Rank Fusion.

    Each list gets its own weight. Default equal weights.
    """
    n = len(result_lists)
    if weights is None:
        weights = [1.0 / n] * n
    else:
        # Normalise
        total = sum(weights)
        if total > 0:
            weights = [w / total for w in weights]

    scored: dict[str, tuple[float, dict[str, Any]]] = {}

    for list_idx, results in enumerate(result_lists):
        w = weights[list_idx] if list_idx < len(weights) else 0
        for rank, item in enumerate(results):
            text = item.get("text", "")
            if not text:
                continue
            rrf_score = w / (k + rank + 1)
            if text in scored:
                old_score, _ = scored[text]
                scored[text] = (old_score + rrf_score, item)
            else:
                scored[text] = (rrf_score, item)

    ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked]


# ---------------------------------------------------------------------------
# Tokenizer (kept for token_count metadata)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    parts = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    return [p for p in parts if p.strip()]


def _escape(s: str) -> str:
    """Escape single quotes for LanceDB SQL filter strings."""
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Context packing (used by agent)
# ---------------------------------------------------------------------------

def pack_retrieved_context(docs: list[Document], max_chars: int) -> str:
    """Compress retrieved documents into a context string.

    Groups results by source → page for coherent context, includes
    section paths, deduplicates, and truncates to *max_chars*.
    """
    if not docs:
        return ""
    max_chars = max(max_chars, 1024)

    parts: list[str] = ["Relevant information from sources:\n\n"]
    total_len = len(parts[0])
    seen: set[str] = set()

    # Group by (source_id, page_number) for coherent context blocks
    from collections import OrderedDict
    groups: OrderedDict[str, list[Document]] = OrderedDict()
    for doc in docs:
        key = f"{doc.metadata.get('source_id', '')}::{doc.metadata.get('page_number', 1)}"
        if key not in groups:
            groups[key] = []
        groups[key].append(doc)

    source_idx = 0
    for _group_key, group_docs in groups.items():
        # Sort sub-chunks within a page by page_chunk_idx
        group_docs.sort(key=lambda d: d.metadata.get("page_chunk_idx", 0))

        for doc in group_docs:
            chunk = doc.page_content.strip()
            if not chunk:
                continue
            normalized = " ".join(chunk.lower().split())
            if normalized in seen:
                continue
            seen.add(normalized)

            source_idx += 1
            source_name = doc.metadata.get("source", "")
            source_id = doc.metadata.get("source_id", "")
            page_number = doc.metadata.get("page_number", 1)
            section_title = doc.metadata.get("section_title", "")
            section_path = doc.metadata.get("section_path", "")

            # Build header with available metadata
            header_parts = [f"[Source {source_idx}]"]
            header_parts.append(f"Source: {source_name}")
            header_parts.append(f"Page: {page_number}")
            if section_path:
                header_parts.append(f"Section: {section_path}")
            elif section_title:
                header_parts.append(f"Section: {section_title}")

            entry = "\n".join(header_parts) + f"\n{chunk}\n\n"

            if total_len + len(entry) > max_chars:
                remaining = max_chars - total_len
                if remaining > 64:
                    parts.append(entry[:remaining])
                break
            parts.append(entry)
            total_len += len(entry)
        else:
            continue
        break  # outer break when inner breaks due to max_chars

    return "".join(parts)
