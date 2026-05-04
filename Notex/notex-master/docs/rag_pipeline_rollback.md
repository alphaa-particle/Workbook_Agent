# Notex RAG Pipeline: Current State and Rollback Guide

Date: 2026-02-26

## 1) Current End-to-End Pipeline (as implemented)

### Request and data flow
1. File upload enters `POST /api/upload` in [backend/server.go](../backend/server.go).
2. Uploaded file is saved under `./data/uploads/<user_id>/<unique_filename>`.
3. Server extracts source content via `VectorStore.ExtractDocument` in [backend/vector.go](../backend/vector.go).
4. Source record is persisted via `Store.CreateSource` in [backend/store.go](../backend/store.go).
5. Source content is synchronously chunked and ingested via `VectorStore.IngestText`.
6. Chat endpoint `POST /api/notebooks/:id/chat` calls `Agent.Chat` in [backend/agent.go](../backend/agent.go).
7. Retrieval uses `VectorStore.SimilaritySearch` (keyword/substring scoring, not dense embeddings).
8. Prompt is assembled with chat history + retrieved chunks and sent to configured LLM.

### Relevant config controls
- LLM/provider: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`
- Retrieval/chunking: `MAX_SOURCES`, `CHUNK_SIZE`, `CHUNK_OVERLAP`
- Conversion: `ENABLE_MARKITDOWN`
- Storage: `VECTOR_STORE_TYPE`, `SQLITE_PATH`, `STORE_PATH`

## 2) Confirmed Failure Modes (pre-fix behavior)

1. **Binary file misread risk**  
   When `ENABLE_MARKITDOWN=false`, binary formats (PDF/DOCX/XLSX/PPTX) were previously vulnerable to direct byte-to-string read behavior, producing garbled text in context.

2. **Duplicate ingestion risk**  
   Upload path ingests immediately; later notebook lazy-load could ingest the same source again, inflating chunk counts and polluting retrieval.

3. **Source deletion drift**  
   Deleting a source from DB did not previously remove its vector chunks from in-memory store.

4. **Retrieval quality ceiling**  
   Current retrieval is lexical scoring over in-memory chunks, without embedding-based semantic retrieval.

## 3) Phase-1 Fixes Applied

1. **Extraction hardening** in [backend/vector.go](../backend/vector.go)  
   - Binary office/PDF formats now require markitdown conversion path.
   - If conversion is disabled for these formats, extraction fails fast with actionable error.
   - Additional binary-content guard added for direct-read files.

2. **Duplicate ingestion guard** in [backend/vector.go](../backend/vector.go)  
   - Added content fingerprinting by `(notebook_id, source_name, content_hash)`.
   - Re-ingest attempts with same fingerprint are skipped.

3. **Delete consistency fix** in [backend/server.go](../backend/server.go) and [backend/vector.go](../backend/vector.go)  
   - Source delete now also removes matching vector chunks by `(notebook_id, source)`.

## 4) Rollback Strategy

### Rollback level A (behavior only, no file revert)
- Set `ENABLE_MARKITDOWN=true` and install markitdown to maximize document extraction quality.
- Keep dedup and delete-sync enabled (safe data integrity improvements).

### Rollback level B (code rollback)
If you must return to pre-change behavior:
1. Revert [backend/vector.go](../backend/vector.go) and [backend/server.go](../backend/server.go) to previous commit.
2. Restart service.
3. Re-ingest affected notebooks if chunk counts were inflated previously.

### Operational warning
Rolling back to old extraction behavior may reintroduce unreadable context for binary files and degrade chat quality.

## 5) Next Modernization Steps (planned)

1. Add hybrid retrieval (BM25 + dense embeddings + rank fusion) with SQLite-first default.
2. Attach stable `source_id` metadata to chunk records and return true source references in chat.
3. Add retrieval/rag regression suite (golden questions, source-grounding checks, latency budget).
4. Introduce small-model context packing and query rewriting for Gemma-class models.

## 6) Validation Checklist

- Upload PDF/DOCX with `ENABLE_MARKITDOWN=true`; verify extracted text readability.
- Ask source-specific questions; verify cited sources are relevant.
- Reopen notebook and chat again; verify chunk counts do not increase unexpectedly.
- Delete a source; verify answers no longer reference deleted content.
