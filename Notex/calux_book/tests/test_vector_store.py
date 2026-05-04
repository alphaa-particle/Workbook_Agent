"""Tests for calux_book.vector_store — LanceDB hybrid search and text processing."""

from __future__ import annotations

import pytest

from calux_book.vector_store import (
    Document,
    VectorStore,
    _rrf_merge_multi,
    _tokenize,
    _escape,
    pack_retrieved_context,
)
from calux_book.parser_router import _is_likely_binary


class TestTokenize:
    def test_empty(self):
        assert _tokenize("") == []

    def test_basic(self):
        tokens = _tokenize("Hello World")
        assert tokens == ["hello", "world"]

    def test_unicode_cjk(self):
        tokens = _tokenize("你好世界 hello")
        assert "hello" in tokens
        # CJK characters should be tokenized
        assert any("\u4e00" <= ch <= "\u9fff" for t in tokens for ch in t)

    def test_strips_punctuation(self):
        tokens = _tokenize("hello, world! how's it going?")
        assert "hello" in tokens
        assert "world" in tokens


class TestEscape:
    def test_no_quotes(self):
        assert _escape("hello") == "hello"

    def test_single_quotes(self):
        assert _escape("it's") == "it''s"


class TestRRFMerge:
    def test_merge_two_lists(self):
        dense = [{"text": "a", "score": 1.0}, {"text": "b", "score": 0.5}]
        fts = [{"text": "b", "score": 1.0}, {"text": "c", "score": 0.5}]
        result = _rrf_merge_multi([dense, fts], weights=[0.55, 0.45], k=60)
        texts = [r.get("text") for r in result]
        # "b" appears in both lists so should get a higher fused score
        assert texts[0] == "b"
        assert len(result) == 3  # a, b, c

    def test_empty_lists(self):
        assert _rrf_merge_multi([[], []], k=60) == []

    def test_single_list(self):
        dense = [{"text": "x"}]
        result = _rrf_merge_multi([dense, []], k=60)
        assert len(result) == 1


class TestIsLikelyBinary:
    def test_text(self):
        assert _is_likely_binary(b"Hello world\nThis is text.") is False

    def test_binary(self):
        assert _is_likely_binary(b"\x00\x01\x02\x03\x89PNG") is True

    def test_empty(self):
        assert _is_likely_binary(b"") is False


class TestVectorStore:
    """VectorStore integration tests with mocked embeddings + real LanceDB."""

    def test_ingest_and_search(self, vector_store):
        vs = vector_store
        n = vs.ingest_source(
            "nb1", "s1", "doc.txt",
            "Python is a programming language. It supports multiple paradigms.",
        )
        assert n > 0

        results = vs.similarity_search("nb1", "programming language", 3)
        assert len(results) > 0
        assert any("programming" in d.page_content.lower() for d in results)

    def test_ingest_deduplication(self, vector_store):
        vs = vector_store
        n1 = vs.ingest_source("nb1", "s1", "d.txt", "Some content here")
        n2 = vs.ingest_source("nb1", "s1", "d.txt", "Some content here")
        assert n1 > 0
        assert n2 == 0  # duplicate

    def test_delete(self, vector_store):
        vs = vector_store
        vs.ingest_source("nb1", "s1", "d.txt", "Some content here for deletion")
        stats_before = vs.get_stats()
        assert stats_before.total_documents > 0

        vs.delete("nb1", "s1", "d.txt")
        stats_after = vs.get_stats()
        assert stats_after.total_documents == 0

    def test_search_empty_store(self, vector_store):
        results = vector_store.similarity_search("nb1", "anything")
        assert results == []

    def test_search_wrong_notebook(self, vector_store):
        vs = vector_store
        vs.ingest_source("nb1", "s1", "d.txt", "content for nb1")
        results = vs.similarity_search("nb2", "content")
        assert len(results) == 0

    def test_get_stats(self, vector_store):
        stats = vector_store.get_stats()
        assert stats.total_documents == 0
        assert stats.dimension > 0

    def test_ingest_empty_content(self, vector_store):
        n = vector_store.ingest_source("nb1", "s1", "empty.txt", "")
        assert n == 0

    def test_extract_document_text_file(self, vector_store, tmp_dir):
        fp = tmp_dir / "test.txt"
        fp.write_text("This is a test document.", encoding="utf-8")
        content = vector_store.extract_document(str(fp))
        assert "test document" in content

    def test_extract_document_binary_fails(self, vector_store, tmp_dir):
        fp = tmp_dir / "binary.bin"
        fp.write_bytes(b"\x00\x01\x02" * 1000)
        with pytest.raises(RuntimeError, match="binary"):
            vector_store.extract_document(str(fp))

    def test_page_marker_ingest_sets_page_metadata(self, vector_store):
        vs = vector_store
        content = """[PAGE 1]\nRevenue was $10M in Q1.\n\n[PAGE 2]\nOperating margin improved to 12%."""
        n = vs.ingest_source("nb1", "s1", "report.md", content)
        assert n >= 2

        docs = vs.similarity_search("nb1", "operating margin", 3)
        assert docs
        assert any(int(d.metadata.get("page_number", 0)) >= 1 for d in docs)

    def test_similarity_search_source_filter(self, vector_store):
        vs = vector_store
        vs.ingest_source("nb1", "s1", "a.txt", "[PAGE 1]\nAlpha budget baseline")
        vs.ingest_source("nb1", "s2", "b.txt", "[PAGE 1]\nBeta structural drawing notes")

        docs = vs.similarity_search("nb1", "structural", 5, source_ids=["s2"])
        assert docs
        assert all(d.metadata.get("source_id") == "s2" for d in docs)


class TestSplitIntoPageChunks:
    """VectorStore._split_into_page_chunks for page-wise chunking."""

    def test_split_into_page_chunks(self, vector_store):
        text = """[PAGE 3]\n# CASH FLOW\nline a\nline b\n\n[PAGE 4]\n| col | val |\n| --- | --- |\n| x | y |"""
        chunks = vector_store._split_into_page_chunks(text, 2000, 100, source_name="report.pdf")
        assert chunks
        pages = {c["page_number"] for c in chunks}
        assert pages == {3, 4}
        assert any(c["block_type"] == "table" for c in chunks)
        # New fields should be present
        assert all("section_path" in c for c in chunks)
        assert all("keywords" in c for c in chunks)
        assert all("context_prefix" in c for c in chunks)

    def test_one_chunk_per_page(self, vector_store):
        text = "[PAGE 1]\n" + " ".join(f"word{i}" for i in range(100)) + "\n\n[PAGE 2]\n" + " ".join(f"word{i}" for i in range(100, 200))
        # Use a chunk_size large enough to fit each page in one chunk
        chunks = vector_store._split_into_page_chunks(text, 2000, 100, source_name="test.txt")
        # Should be exactly 2 chunks — one per page, no sub-splitting
        assert len(chunks) == 2
        assert chunks[0]["page_number"] == 1
        assert chunks[1]["page_number"] == 2

    def test_sub_chunking_on_long_page(self, vector_store):
        """When a page exceeds chunk_size, it should be split into sub-chunks."""
        long_text = ". ".join(f"Sentence number {i} with some extra words" for i in range(50))
        text = f"[PAGE 1]\n{long_text}"
        chunks = vector_store._split_into_page_chunks(text, 200, 50, source_name="long.pdf")
        assert len(chunks) > 1
        assert all(c["page_number"] == 1 for c in chunks)
        # page_chunk_idx should be sequential
        idxs = [c["page_chunk_idx"] for c in chunks]
        assert idxs == list(range(len(chunks)))

    def test_section_path_propagated(self, vector_store):
        """Section headings should propagate into section_path."""
        text = "[PAGE 1]\n# Introduction\nSome intro text.\n\n[PAGE 2]\nMore text under intro."
        chunks = vector_store._split_into_page_chunks(text, 2000, 100, source_name="doc.pdf")
        assert len(chunks) >= 2
        # Page 1 should have Introduction in section_path
        assert "Introduction" in chunks[0].get("section_path", "")


class TestPackRetrievedContext:
    def test_basic(self):
        docs = [
            Document(page_content="First chunk", metadata={"source": "s1", "source_id": "id1", "page_number": 1}),
            Document(page_content="Second chunk", metadata={"source": "s2", "source_id": "id2", "page_number": 2}),
        ]
        result = pack_retrieved_context(docs, 10000)
        assert "First chunk" in result
        assert "Second chunk" in result
        assert "Source 1" in result or "[Source 1]" in result
        assert "Page: 1" in result

    def test_deduplication(self):
        docs = [
            Document(page_content="same content", metadata={"source": "s1", "source_id": "id1", "page_number": 1}),
            Document(page_content="same content", metadata={"source": "s1", "source_id": "id1", "page_number": 1}),
        ]
        result = pack_retrieved_context(docs, 10000)
        assert result.count("same content") == 1

    def test_truncation(self):
        docs = [
            Document(page_content="x" * 5000, metadata={"source": "s1", "source_id": "id1", "page_number": 1}),
        ]
        # min max_chars is 1024 (enforced by pack_retrieved_context)
        result = pack_retrieved_context(docs, 1500)
        assert len(result) <= 1600  # some overhead for headers
        assert len(result) < 5000  # significantly shorter than input

    def test_empty(self):
        assert pack_retrieved_context([], 1000) == ""

    def test_grouping_by_source_and_page(self):
        """Chunks from same source+page should be grouped together."""
        docs = [
            Document(page_content="Chunk A", metadata={"source": "doc.pdf", "source_id": "s1", "page_number": 1, "page_chunk_idx": 0}),
            Document(page_content="Chunk B", metadata={"source": "doc.pdf", "source_id": "s1", "page_number": 1, "page_chunk_idx": 1}),
            Document(page_content="Chunk C", metadata={"source": "other.pdf", "source_id": "s2", "page_number": 5}),
        ]
        result = pack_retrieved_context(docs, 10000)
        # Chunk A and B should appear before Chunk C (grouped by source+page)
        pos_a = result.find("Chunk A")
        pos_b = result.find("Chunk B")
        pos_c = result.find("Chunk C")
        assert pos_a < pos_b < pos_c

    def test_section_path_in_header(self):
        """Section path should appear in the header when available."""
        docs = [
            Document(page_content="Content", metadata={
                "source": "book.pdf", "source_id": "s1", "page_number": 3,
                "section_path": "Chapter 1 > Introduction",
            }),
        ]
        result = pack_retrieved_context(docs, 10000)
        assert "Section: Chapter 1 > Introduction" in result
