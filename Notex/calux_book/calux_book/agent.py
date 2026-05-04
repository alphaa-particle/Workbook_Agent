"""AI Agent for Calux Book — transformations and RAG chat.

Orchestrates the LLM provider, vector store, and prompt templates to
generate summaries, custom notes, and conversational responses.

Supports **map-reduce summarisation** for large documents: when the full
document content exceeds the context window, it is split into batches,
each batch is summarised independently, and the batch summaries are
combined into one final summary.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

from .config import Settings
from .models import (
    ChatMessage,
    ChatResponse,
    Source,
    SourceSummary,
    TransformationRequest,
    TransformationResponse,
)
from .prompts import chat_system_prompt, get_transformation_prompt
from .providers import LLMProvider, create_provider, create_text_provider
from .vector_store import VectorStore, Document, pack_retrieved_context

logger = logging.getLogger("calux_book.agent")


class Agent:
    """Handles AI-powered transformations and RAG chat."""

    def __init__(
        self, cfg: Settings, vector_store: VectorStore, provider: LLMProvider,
        text_provider: LLMProvider,
    ) -> None:
        self.cfg = cfg
        self.vector_store = vector_store
        self.provider = provider
        self.text_provider = text_provider

    # ------------------------------------------------------------------
    # Transformations
    # ------------------------------------------------------------------

    async def generate_transformation(
        self, req: TransformationRequest, sources: list[Source],
    ) -> TransformationResponse:
        """Generate a note from sources using the specified transformation type.

        For **summaries** the full document content is used (not just a
        similarity-search sample).  If the content exceeds the context
        window a map-reduce strategy is applied automatically.
        """
        limit = self.cfg.max_context_length or 100_000

        notebook_id = sources[0].notebook_id if sources else ""
        source_filter = req.source_ids or [s.id for s in sources if s.id]

        # ── Retrieve context ────────────────────────────────────────
        if req.type == "summary":
            # For summaries, retrieve ALL chunks in page order so the
            # LLM sees the complete document — not a 10-chunk sample.
            all_docs = self.vector_store.get_all_chunks(
                notebook_id, source_filter,
            )
            logger.info(
                "Summary transform: %d total chunks for %d sources",
                len(all_docs), len(source_filter),
            )
            source_text = pack_retrieved_context(all_docs, limit)

            # If total content is large, use map-reduce summarisation
            total_chars = sum(len(d.page_content) for d in all_docs)
            if total_chars > int(limit * 0.7):
                logger.info(
                    "Content %d chars exceeds 70%% of context limit %d "
                    "— using map-reduce summarisation",
                    total_chars, limit,
                )
                response = await self._map_reduce_summary(
                    all_docs, req, sources, limit,
                )
                source_summaries = [
                    SourceSummary(id=s.id, name=s.name, type=s.type)
                    for s in sources
                ]
                return TransformationResponse(
                    type=req.type,
                    content=response,
                    sources=source_summaries,
                    created_at=datetime.utcnow(),
                    metadata={"length": req.length, "format": req.format,
                              "strategy": "map_reduce",
                              "total_chunks": len(all_docs)},
                )
        else:
            # For non-summary transforms, use similarity search
            query = " ".join(
                part for part in [req.prompt.strip(), req.type, req.length, req.format] if part
            ).strip() or "summarize key findings"
            all_docs = self.vector_store.similarity_search(
                notebook_id,
                query,
                max(self.cfg.max_sources * 4, 20),
                source_filter,
            )
            source_text = pack_retrieved_context(all_docs, limit)

        if not source_text:
            listed = "\n".join(f"- {s.name} ({s.type})" for s in sources)
            source_text = (
                "Relevant information from sources:\n\n"
                "No indexed chunks matched this request yet.\n"
                "Available sources:\n"
                f"{listed}\n"
            )

        template = get_transformation_prompt(req.type)
        prompt = template.format(
            sources=source_text,
            type=req.type,
            length=req.length,
            format=req.format,
            prompt=req.prompt,
        )

        response = await self.text_provider.generate_from_prompt(prompt)

        source_summaries = [
            SourceSummary(id=s.id, name=s.name, type=s.type) for s in sources
        ]

        return TransformationResponse(
            type=req.type,
            content=response,
            sources=source_summaries,
            created_at=datetime.utcnow(),
            metadata={"length": req.length, "format": req.format,
                       "total_chunks": len(all_docs)},
        )

    # ------------------------------------------------------------------
    # Hierarchical tree summarisation (for large documents)
    # ------------------------------------------------------------------

    async def _map_reduce_summary(
        self,
        all_docs: list[Document],
        req: TransformationRequest,
        sources: list[Source],
        context_limit: int,
    ) -> str:
        """Summarise a large document via hierarchical tree reduction.

        Instead of the flat map→reduce that produced 33+ sequential
        LLM-call rounds for large books, this uses a multi-level tree:

        1. **Map phase** — split chunks into batches that each fit in
           ~80 % of the context window, summarise each batch.
        2. **Hierarchical reduce** — group batch summaries into groups
           of N (default 5), produce a group summary for each, then
           repeat until a single summary remains.

        This limits the reduce depth to ~2 levels even for very large
        documents, and runs batches highly parallel.
        """
        batch_fill = getattr(self.cfg, "summary_batch_fill", 0.80)
        concurrency = getattr(self.cfg, "summary_concurrency", 6)
        max_batches = getattr(self.cfg, "summary_max_batches", 40)
        group_size = getattr(self.cfg, "summary_group_size", 5)

        batch_limit = int(context_limit * batch_fill)

        # --- Map phase: build batches ---
        batches: list[list[Document]] = []
        current_batch: list[Document] = []
        current_len = 0

        for doc in all_docs:
            doc_len = len(doc.page_content) + 120  # overhead for metadata
            if current_len + doc_len > batch_limit and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_len = 0
            current_batch.append(doc)
            current_len += doc_len

        if current_batch:
            batches.append(current_batch)

        # Cap batches — if too many, merge adjacent batches
        if len(batches) > max_batches:
            logger.info(
                "Capping %d batches to %d by merging adjacent batches",
                len(batches), max_batches,
            )
            factor = math.ceil(len(batches) / max_batches)
            merged: list[list[Document]] = []
            for i in range(0, len(batches), factor):
                group = []
                for b in batches[i : i + factor]:
                    group.extend(b)
                merged.append(group)
            batches = merged

        logger.info(
            "Hierarchical summary: %d total chunks → %d batches "
            "(limit %d chars/batch, concurrency %d)",
            len(all_docs), len(batches), batch_limit, concurrency,
        )

        # --- Map phase: summarise each batch ---
        map_prompt_template = (
            "You are an expert summariser. Summarise the following section of "
            "a document faithfully and comprehensively. Preserve key facts, "
            "figures, section headings, and important details.\n"
            "**Important: Always respond in English. Do not wrap the output in "
            "```markdown``` tags.**\n\n"
            "Section content:\n{sources}\n\n"
            "Produce a detailed {length} summary in {format} format."
        )

        semaphore = asyncio.Semaphore(concurrency)

        async def _summarise_batch(batch_docs: list[Document], idx: int, total: int) -> str:
            packed = pack_retrieved_context(batch_docs, batch_limit)
            prompt = map_prompt_template.format(
                sources=packed,
                length=req.length,
                format=req.format,
            )
            logger.info("Map phase: summarising batch %d/%d (%d chunks)",
                        idx + 1, total, len(batch_docs))
            async with semaphore:
                return await self.text_provider.generate_from_prompt(prompt)

        batch_summaries: list[str] = list(await asyncio.gather(
            *[_summarise_batch(b, i, len(batches)) for i, b in enumerate(batches)]
        ))

        logger.info(
            "Map phase complete: %d batch summaries produced "
            "(total %d chars)",
            len(batch_summaries),
            sum(len(s) for s in batch_summaries),
        )

        # --- Hierarchical reduce phase ---
        summaries = [s for s in batch_summaries if s.strip()]
        level = 1

        while len(summaries) > 1:
            # If they all fit in one context window, do a single final merge
            combined_len = sum(len(s) for s in summaries) + 50 * len(summaries)
            if combined_len <= context_limit * 0.85:
                break  # → final reduce below

            # Group summaries and reduce each group
            groups: list[list[str]] = []
            for i in range(0, len(summaries), group_size):
                groups.append(summaries[i : i + group_size])

            if len(groups) == len(summaries):
                # Each group has only 1 item — nothing to reduce further
                break

            logger.info(
                "Reduce level %d: %d summaries → %d groups (group_size=%d)",
                level, len(summaries), len(groups), group_size,
            )

            group_prompt_template = (
                "You are an expert at creating comprehensive summaries. "
                "Below are summaries of consecutive sections of a document. "
                "Combine them into a single coherent summary that preserves "
                "all key facts, figures, and important details.\n"
                "**Important: Always respond in English. Do not wrap the "
                "output in ```markdown``` tags.**\n\n"
                "Section summaries:\n{sources}\n\n"
                "Produce a well-structured detailed summary in {format} format."
            )

            async def _reduce_group(group: list[str], idx: int) -> str:
                combined = "\n\n---\n\n".join(
                    f"**Section {i+1}:**\n{s}" for i, s in enumerate(group)
                )
                prompt = group_prompt_template.format(
                    sources=combined,
                    format=req.format,
                )
                logger.info("Reduce level %d: group %d/%d (%d summaries)",
                            level, idx + 1, len(groups), len(group))
                async with semaphore:
                    return await self.text_provider.generate_from_prompt(prompt)

            summaries = [
                s for s in await asyncio.gather(
                    *[_reduce_group(g, i) for i, g in enumerate(groups)]
                )
                if s.strip()
            ]
            level += 1

            # Safety valve — don't recurse more than 4 levels
            if level > 4:
                logger.warning("Reduce reached %d levels, forcing final merge", level)
                break

        # --- Final reduce ---
        combined = "\n\n---\n\n".join(
            f"**Section {i+1} Summary:**\n{s}"
            for i, s in enumerate(summaries)
        )

        reduce_prompt = (
            "You are an expert at creating comprehensive summaries. "
            "Below are summaries of different sections of a document. "
            "Combine them into a single coherent {length} summary in "
            "{format} format that covers the entire document.\n"
            "**Important: Always respond in English. Do not wrap the "
            "output in ```markdown``` tags.**\n\n"
            "Section summaries:\n{sources}\n\n"
            "Produce a well-structured final summary that captures all "
            "key information, main topics, and important details from "
            "every section."
        ).format(
            sources=combined,
            length=req.length,
            format=req.format,
        )

        final = await self.text_provider.generate_from_prompt(reduce_prompt)
        logger.info("Final reduce complete (%d chars, %d levels)", len(final), level)
        return final

    # ------------------------------------------------------------------
    # Chat (RAG)
    # ------------------------------------------------------------------

    async def chat(
        self, notebook_id: str, message: str, history: list[ChatMessage],
    ) -> ChatResponse:
        """Perform RAG-powered chat against a notebook's vector store.

        Uses two-stage hybrid retrieval (dense + BM25 + reranking) with
        context expansion for large-book sources.
        """
        # Similarity search with reranking + sibling expansion
        docs = self.vector_store.similarity_search(
            notebook_id, message, self.cfg.max_sources,
        )

        # Pack context — use a sensible limit for the LLM's context window
        # For Ollama local models (~8K context), 6000 chars ≈ 1500 tokens
        ctx_limit = self.cfg.max_context_length if self.cfg.max_context_length > 0 else 6000
        context_text = pack_retrieved_context(docs, ctx_limit)

        # Build chat history (limit to last 6 messages to save tokens on
        # local models with small context windows)
        max_history = 6 if self.cfg.is_ollama else 10
        history_lines: list[str] = []
        for i, msg in enumerate(history[-max_history:]):
            role = "User" if msg.role == "user" else "Assistant"
            history_lines.append(f"{role}: {msg.content}")

        template = chat_system_prompt()
        prompt = template.format(
            history="\n".join(history_lines),
            context=context_text,
            question=message,
        )

        response = await self.text_provider.generate_from_prompt(prompt)

        # Build source summaries (deduplicated, ordered by first appearance)
        source_summaries: list[SourceSummary] = []
        seen: set[str] = set()
        for doc in docs:
            source_name = doc.metadata.get("source", "")
            source_id = doc.metadata.get("source_id", "") or source_name
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            source_summaries.append(SourceSummary(
                id=source_id,
                name=source_name or source_id,
                type="file",
            ))

        return ChatResponse(
            message=response,
            sources=source_summaries,
            session_id=notebook_id,
            metadata={
                "docs_retrieved": len(docs),
                "context_chars": len(context_text),
            },
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def generate_summary(
        self, sources: list[Source], length: str = "medium",
    ) -> str:
        req = TransformationRequest(type="summary", length=length, format="markdown")
        resp = await self.generate_transformation(req, sources)
        return resp.content


def create_agent(cfg: Settings, vector_store: VectorStore) -> Agent:
    """Factory to build an Agent with the right provider chain."""
    provider = create_provider(cfg)
    text_provider = create_text_provider(cfg)
    # If the image provider can do text, use it as text_provider too
    if cfg.image_provider == "gemini":
        # GeminiProvider delegates text to its internal text_provider
        pass
    return Agent(cfg, vector_store, provider, text_provider)
