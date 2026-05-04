# Calux Book: Visual Architecture & System Design

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                            │
│                          🖥️  FRONTEND (SPA)                              │
│                    Single-page app (Vanilla JS)                          │
│              📓 Notebooks | 📄 Sources | 💬 Chat | 📝 Notes             │
│                                                                            │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ REST API (JSON)
        ┌────────────────────▼──────────────────────┐
        │                                           │
        │        🚀 FASTAPI SERVER (server.py)     │
        │                                           │
        │  ┌─────────────────────────────────────┐ │
        │  │ ⚙️  AuditMiddleware                │ │
        │  │    • Extract user_id (JWT/cookie) │ │
        │  │    • Log all activity             │ │
        │  └─────────────────────────────────────┘ │
        │                                           │
        │  Routes:                                  │
        │  ├─ 📓 /notebooks (CRUD)                 │
        │  ├─ 📄 /sources (upload/ingest)         │
        │  ├─ 💬 /chat (Q&A with retrieval)       │
        │  ├─ 📝 /transform (summarize)           │
        │  └─ 🔐 /auth (OAuth callbacks)          │
        │                                           │
        └────────────────┬──────────────────────────┘
                         │
        ┌────────────────┴───────────────────┐
        │                                    │
   ┌────▼─────────┐            ┌───────────▼──────────┐
   │               │            │                      │
   │  🤖 AGENT     │            │  🔐 AUTH LAYER      │
   │ (agent.py)   │            │ (auth.py)           │
   │               │            │                      │
   │ • Transforms  │            │ • OAuth (GitHub,    │
   │ • RAG Chat    │            │   Google)           │
   │ • Map-Reduce  │            │ • JWT generation    │
   │   Summarize   │            │ • Guest sessions    │
   │               │            │                      │
   └────┬──────────┘            └──────────────────────┘
        │
        │ Coordinates:
        │ 1. Retrieve context
        │ 2. Build prompts  
        │ 3. Call LLM
        │ 4. Store results
        │
   ┌────▼──────────────────────────────────────────────────────┐
   │                                                            │
   │         📊 DATA ACCESS & RETRIEVAL LAYER                 │
   │                                                            │
   │  ┌──────────────────────┐    ┌──────────────────────┐   │
   │  │  Store (store.py)    │    │ VectorStore          │   │
   │  │  SQLite              │    │ (vector_store.py)    │   │
   │  │                      │    │ LanceDB              │   │
   │  │ • Notebooks          │    │                      │   │
   │  │ • Sources            │    │ Hybrid Retrieval:    │   │
   │  │ • Notes              │    │ ├─ Dense search      │   │
   │  │ • Chats              │    │ │  (ANN via fastem)  │   │
   │  │ • Users              │    │ ├─ Sparse search     │   │
   │  │ • Activity logs      │    │ │  (BM25 via tanty)  │   │
   │  │ • Activity logs      │    │ ├─ RRF merge         │   │
   │  │                      │    │ ├─ Rerank            │   │
   │  │ [TTL Cache Wrapper]  │    │ │  (cross-encoder)   │   │
   │  │                      │    │ └─ Context packing   │   │
   │  └──────────────────────┘    └──────────────────────┘   │
   │                                                            │
   └────┬──────────────────────────────────────────────────────┘
        │
        │ Uses:
        │ 1. Parser & Embeddings
        │ 2. LLM Providers
        │
   ┌────▼─────────────────────────────────────────────────────────┐
   │                                                               │
   │         🛠️  SUPPORTING COMPONENTS                           │
   │                                                               │
   │  ┌────────────────────┐  ┌────────────────┐  ┌────────────┐ │
   │  │ 📋 ParserRouter    │  │ 🔢 Embedding   │  │ 🧠 LLM      │ │
   │  │ (parser_router.py) │  │ (embedding.py) │  │ Providers   │ │
   │  │                    │  │                │  │ (providers) │ │
   │  │ Extract text from: │  │ • Dense:       │  │             │ │
   │  │ ├─ DOCX            │  │   fastembed    │  │ • OpenAI    │ │
   │  │ ├─ PDF (pdfium)    │  │   (BAAI/bge)   │  │ • Gemini    │ │
   │  │ ├─ XLSX/CSV        │  │   384-dim      │  │ • Ollama    │ │
   │  │ ├─ Images (OCR)    │  │ • Sparse:      │  │ • GLM       │ │
   │  │ │  rapidocr        │  │   Tantivy BM25 │  │ • ZImage    │ │
   │  │ └─ TXT             │  │ • Reranker:    │  │ (Abstraction│ │
   │  │                    │  │   ms-marco     │  │  layer)     │ │
   │  │ + OCR Fallback     │  │   MiniLM       │  │             │ │
   │  │                    │  │   (~22MB)      │  │             │ │
   │  └────────────────────┘  └────────────────┘  └────────────┘ │
   │                                                               │
   │  ┌────────────────────────────────────────────────────────┐  │
   │  │ ⚙️  Config & Hardware Tuning (config.py, hardware.py) │  │
   │  │                                                        │  │
   │  │ Settings from environment: .env → .env.local        │  │
   │  │ Auto-tune for hw: CPU cores, RAM, GPU detection     │  │
   │  │ Adjust: embedding batch size, threads, model size  │  │
   │  └────────────────────────────────────────────────────────┘  │
   │                                                               │
   └────┬──────────────────────────────────────────────────────────┘
        │
        │
   ┌────▼──────────────────────────────────────────────────────────┐
   │                                                                │
   │         💾 PERSISTENCE LAYER (on disk)                        │
   │                                                                │
   │  ┌────────────────┐      ┌────────────────────────────────┐  │
   │  │ 📊 SQLite DB   │      │ 🔍 LanceDB Vector Index        │  │
   │  │ data/          │      │ data/lancedb/                  │  │
   │  │ checkpoints.db │      │ chunks.lance/                  │  │
   │  │                │      │                                │  │
   │  │ • Structured   │      │ • Dense vectors (384-dim)      │  │
   │  │   data         │      │ • Sparse vectors (BM25 tokens) │  │
   │  │ • ACID         │      │ • Chunk metadata               │  │
   │  │   properties   │      │ • Page/section info            │  │
   │  │ • Single file  │      │ • Persistent ANN index         │  │
   │  │                │      │ • Tantivy BM25 index           │  │
   │  │                │      │                                │  │
   │  └────────────────┘      └────────────────────────────────┘  │
   │                                                                │
   │  ┌──────────────────────────────────────────────────────────┐ │
   │  │ 📁 File System                                           │ │
   │  │ data/uploads/{guest_id}/    → User-uploaded files      │ │
   │  │ logs/audit.log.*            → Rotating audit logs      │ │
   │  └──────────────────────────────────────────────────────────┘ │
   │                                                                │
   └────────────────────────────────────────────────────────────────┘
```

---

## Request Lifecycle: Chat Message

```
User types: "Summarize the main points"
│
├─ [1] HTTP POST /api/notebooks/{notebook_id}/chat
│      {message: "Summarize...", session_id: "..."}
│
├─ [2] AuditMiddleware intercepts
│      • Extract user_id from Authorization header (JWT)
│      • Verify JWT token validity (decode, check expiry)
│      • Log: {user_id: "123", action: "chat", timestamp, ip}
│
├─ [3] Route Handler: server.py → chat()
│      • Extract & validate notebook_id
│      • Authorize: Does user own this notebook?
│      • Call agent.generate_chat_response()
│
├─ [4] Agent Layer: agent.py → generate_chat_response()
│
│      ┌─ RETRIEVAL PHASE ─────────────────────────────┐
│      │                                                 │
│      ├─ [4a] vector_store.hybrid_search(               │
│      │         query="Summarize...",                  │
│      │         notebook_id="123"                      │
│      │       )                                         │
│      │                                                 │
│      │  Step 1: Dense Search                          │
│      │  ├─ Convert query to embedding (fastembed)     │
│      │  ├─ Query LanceDB ANN index                    │
│      │  ├─ Return top 100 chunks by vector similarity │
│      │                                                 │
│      │  Step 2: Sparse Search                         │
│      │  ├─ Parse query: tokenize, remove stopwords    │
│      │  ├─ Query BM25 index (tantivy)                 │
│      │  ├─ Return top 100 chunks by term frequency    │
│      │                                                 │
│      │  Step 3: RRF Merge                             │
│      │  ├─ For each chunk: score =                    │
│      │  │   1/(60+dense_rank) + 1/(60+sparse_rank)    │
│      │  ├─ Sort by score, take top 20                 │
│      │                                                 │
│      │  Step 4: Reranking                             │
│      │  ├─ Cross-encoder model (ms-marco-MiniLM)      │
│      │  ├─ Score each (query, chunk) pair            │
│      │  ├─ Return top 8 by rerank score               │
│      │                                                 │
│      └─ Result: 8 most relevant chunks ────────────────┘
│
│      ┌─ CONTEXT PACKING ─────────────────────────────┐
│      │ Concatenate chunks into context window:        │
│      │ max_context_length = 6000 chars (60% of tokens)│
│      │ Result: "Context: chunk1\n---\nchunk2\n..."   │
│      └────────────────────────────────────────────────┘
│
│      ┌─ PROMPT BUILDING ─────────────────────────────┐
│      │ prompt = f"""                                  │
│      │ {chat_system_prompt}                           │
│      │                                                │
│      │ Context:                                       │
│      │ {packed_context}                               │
│      │                                                │
│      │ User: Summarize the main points                │
│      │ Assistant:"""                                  │
│      └────────────────────────────────────────────────┘
│
├─ [5] LLM Generation: providers.py → generate_from_prompt()
│      • Route to provider (OpenAI/Gemini/Ollama)
│      • Send prompt via API/local server
│      • Receive response text
│      • LLM ONLY sees: {context + query} (no hallucination room)
│
├─ [6] Response Processing
│      • Store ChatMessage in SQLite:
│      │  {session_id, role: "assistant", content, sources: [chunk_ids]}
│      • Extract source IDs from retrieved chunks
│
├─ [7] Return to Frontend
│      • JSON: {message: "response...", sources: ["src1", "src2"], ...}
│      • HTTP 200
│
└─ [8] Frontend Display
       • Show response text
       • Show "Sources: file1.pdf, file2.txt"
       • Allow user to click → view source chunks
```

---

## Data Flow: Source Ingestion

```
User uploads PDF: "research.pdf"
│
├─ [1] HTTP POST /api/notebooks/{id}/sources
│      {name: "research", type: "file", file: <binary>}
│
├─ [2] Middleware → User validation
│
├─ [3] Route: Save file & create source record
│      • Store file at: data/uploads/{guest_id}/research_abc123.pdf
│      • SQLite: INSERT Source {
│          id: "src_123",
│          notebook_id: "nb_456",
│          name: "research",
│          type: "file",
│          status: "pending",
│          content_hash: "<SHA256>",
│          chunk_count: 0
│        }
│      • Return: {source_id: "src_123", status: "pending"}
│
├─ [4] Frontend starts polling: GET /sources/{src_123}
│      every 2 seconds → check status
│
├─ [5] Background Task Triggered
│      
│      ┌─ EXTRACTION PHASE ──────────────────────┐
│      │                                          │
│      ├─ Update status → "extracting"           │
│      │                                          │
│      ├─ ParserRouter.extract_document()        │
│      │  ├─ Detect format: .pdf                 │
│      │  ├─ Try native parser:                  │
│      │  │  pypdfium2.open("research.pdf")      │
│      │  │  → iterate pages → extract text      │
│      │  │  → combine into single string        │
│      │  ├─ If empty → try OCR (rapidocr)       │
│      │  │  └─ convert pages to images          │
│      │  │  └─ run OCR on each page             │
│      │  │  └─ combine results                  │
│      │  └─ Return: extracted_text, metadata    │
│      │                                          │
│      │ Result: "Introduction: This paper...\n  │
│      │          Related Work: Previous studies │
│      │          Methods: We used..."           │
│      │                                          │
│      └─────────────────────────────────────────┘
│
│      ┌─ CHUNKING PHASE ────────────────────────┐
│      │                                          │
│      ├─ Split text into chunks:                │
│      │  chunk_size: 800 chars                  │
│      │  chunk_overlap: 128 chars               │
│      │                                          │
│      │  Example:                               │
│      │  ├─ Chunk#1: [0:800]                    │
│      │  ├─ Chunk#2: [672:1472]  (overlap)     │
│      │  ├─ Chunk#3: [1344:2144]                │
│      │  └─ ...                                 │
│      │                                          │
│      │ Result: 127 chunks (for 50-page PDF)   │
│      │                                          │
│      └─────────────────────────────────────────┘
│
│      ┌─ EMBEDDING PHASE ───────────────────────┐
│      │                                          │
│      ├─ Load fastembed model (384-dim):        │
│      │  BAAI/bge-small-en-v1.5                 │
│      │  (cached in home/.cache/huggingface)    │
│      │                                          │
│      ├─ Batch embed chunks (batch_size=16):    │
│      │  └─ For chunk in chunks[0:16]:          │
│      │     └─ vec = embedding_engine.embed()   │
│      │     └─ Result: [0.12, -0.34, ..., 0.8] │
│      │        (384 floats)                     │
│      │                                          │
│      ├─ Sparse embed (BM25):                   │
│      │  └─ Tokenize chunk text                 │
│      │  └─ Compute term frequencies            │
│      │  └─ Result: {token_id: score, ...}      │
│      │                                          │
│      └─────────────────────────────────────────┘
│
│      ┌─ STORAGE PHASE ─────────────────────────┐
│      │                                          │
│      ├─ Write to LanceDB table "chunks":       │
│      │  FOR EACH chunk:                        │
│      │    INSERT {                             │
│      │      chunk_id: "chunk_src123_1",        │
│      │      source_id: "src_123",              │
│      │      notebook_id: "nb_456",             │
│      │      page_content: "...",               │
│      │      page_number: 1,                    │
│      │      page_chunk_idx: 0,                 │
│      │      dense_vector: [0.12, ...],         │
│      │      sparse_vector: {...},              │
│      │      created_at: now()                  │
│      │    }                                    │
│      │                                          │
│      ├─ LanceDB creates/updates indices:       │
│      │  ├─ ANN index on dense_vector           │
│      │  └─ BM25 index on page_content via      │
│      │     Tantivy                             │
│      │                                          │
│      └─────────────────────────────────────────┘
│
│      ┌─ UPDATE SOURCE RECORD ──────────────────┐
│      │                                          │
│      ├─ Update SQLite Source:                  │
│      │  UPDATE Source SET {                    │
│      │    status: "ready",                     │
│      │    chunk_count: 127,                    │
│      │    updated_at: now()                    │
│      │  } WHERE id = "src_123"                 │
│      │                                          │
│      └─────────────────────────────────────────┘
│
├─ [6] Frontend detects status → "ready"
│      Display: "✓ Ingested 127 chunks"
│      → User can now query/summarize this source
│
└─ [7] Polling stops
```

---

## Hybrid Retrieval: Dense vs Sparse vs RRF

```
Query: "machine learning performance"

┌─────────────────────────────────────────────────────────────────┐
│ DENSE SEARCH (Semantic Similarity)                              │
├─────────────────────────────────────────────────────────────────┤
│ • Convert query to embedding: "machine learning..." → [0.1...] │
│ • LanceDB ANN index: k-d tree / HNSW                          │
│ • Find 100 nearest neighbors in vector space                   │
│ • Ranking by cosine distance                                   │
│                                                                  │
│ Results:                                                         │
│ ├─ Rank 1: "artificial intelligence algorithms" (score 0.92)   │
│ ├─ Rank 2: "deep learning models" (score 0.88)                │
│ ├─ Rank 3: "neural network performance" (score 0.85)          │
│ ├─ ...                                                          │
│ └─ Rank 100: "database optimization" (score 0.45)             │
│                                                                  │
│ ❌ Problem: Misses exact keyword "performance" if it's rare    │
│ ✅ Good at: Understanding meaning, synonyms, semantic nuance   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ SPARSE SEARCH (Lexical Matching via BM25)                       │
├─────────────────────────────────────────────────────────────────┤
│ • Tokenize query: ["machine", "learning", "performance"]       │
│ • BM25 algorithm (Tantivy): term frequency × IDF               │
│ • Find chunks matching any/all terms                            │
│ • Ranking by statistical relevance                              │
│                                                                  │
│ Results:                                                         │
│ ├─ Rank 1: "machine learning model performance metrics" 0.89   │
│ ├─ Rank 2: "performance optimization in machine learning" 0.87 │
│ ├─ Rank 3: "ensemble learning performance comparison" 0.84    │
│ ├─ ...                                                          │
│ └─ Rank 100: "learning curves and model performance" 0.20     │
│                                                                  │
│ ✅ Good at: Exact keyword matching, named entities             │
│ ❌ Problem: Can't understand that "AI" ≈ "artificial intel"    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ RECIPROCAL RANK FUSION (RRF): Merge Dense + Sparse             │
├─────────────────────────────────────────────────────────────────┤
│ For each document, compute:                                      │
│   rrf_score = 1/(k + dense_rank) + 1/(k + sparse_rank)          │
│   where k=60 (smoothing constant)                                │
│                                                                  │
│ Example scoring:                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ "ensemble learning performance"                             │ │
│ │ ├─ Dense rank: 45 → score = 1/(60+45) = 0.0090            │ │
│ │ ├─ Sparse rank: 3 → score = 1/(60+3) = 0.0159             │ │
│ │ ├─ TOTAL = 0.0249 (HIGH) ← Both methods agree!            │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ "neural network training"                                   │ │
│ │ ├─ Dense rank: 15 → score = 1/(60+15) = 0.0133            │ │
│ │ ├─ Sparse rank: 234 → score = 1/(60+234) = 0.0036         │ │
│ │ ├─ TOTAL = 0.0169 (MEDIUM) ← Dense found it, sparse didn't│ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ "data preprocessing steps"                                  │ │
│ │ ├─ Dense rank: 987 → score = 1/(60+987) = 0.0009          │ │
│ │ ├─ Sparse rank: 2 → score = 1/(60+2) = 0.0152             │ │
│ │ ├─ TOTAL = 0.0161 (MEDIUM) ← Sparse found it, dense missed│ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│ Final ranking (top 20 by RRF score):                            │
│ 1. "ensemble learning performance" (0.0249)                    │
│ 2. "machine learning performance metrics" (0.0198)             │
│ 3. "learning curves and model performance" (0.0185)           │
│ ...                                                              │
│                                                                  │
│ ✅ Combines strengths of both methods!                          │
│ ✅ Reduces false positives + increases precision               │
│ ⚠️ Trade-off: +200ms latency (must run both searches)          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ RERANKING (Optional, but recommended)                           │
├─────────────────────────────────────────────────────────────────┤
│ Take top 20 from RRF → pass through cross-encoder              │
│ Model: Xenova/ms-marco-MiniLM-L-6-v2 (22MB)                   │
│                                                                  │
│ For each (query, document pair):                                │
│   score = cross_encoder(query, document)  ∈ [0, 1]            │
│                                                                  │
│ Reranked results (top 8):                                       │
│ 1. "machine learning model performance evaluation" (0.95)       │
│ 2. "ensemble learning performance metrics" (0.92)              │
│ 3. "performance metrics for machine learning" (0.89)           │
│ ...                                                              │
│ 8. "neural network training techniques" (0.71)                 │
│                                                                  │
│ ✅ Dramatically improves top-k quality                          │
│ ⚠️ Trade-off: +100ms latency (cross-encoder is slower)         │
└─────────────────────────────────────────────────────────────────┘

FINAL RESULT: 8 most relevant chunks
├─ No hallucinations (grounded in source)
├─ Captures both semantic meaning + exact keywords
├─ Ranked by quality via cross-encoder
└─ Passed to LLM as context
```

---

## Authorization Model

```
Current (Simple):

┌──────────────────────────────────────────────────┐
│ Notebook                                         │
├──────────────────────────────────────────────────┤
│ • user_id: "user_123" (owner)                    │
│ • is_public: false (default)                     │
│ • public_token: "tok_abc123" (for sharing)       │
└──────────────────────────────────────────────────┘

Access Rules:
├─ Owner (user_123)
│  ├─ Can read/write/delete notebook
│  ├─ Can share via public token
│  └─ Can create sources, notes, chats
├─ Public reader (anyone with public_token)
│  ├─ Can read notebook + sources
│  └─ Cannot write/modify
└─ Others
   └─ Cannot access


Future (RBAC):

┌─────────────────────────────────────────────────┐
│ Notebook                                        │
├─────────────────────────────────────────────────┤
│ • user_id: "user_123" (owner)                   │
│ • share_settings: [                             │
│     {user: "user_456", role: "editor"},         │
│     {user: "user_789", role: "viewer"},         │
│     {public: true, role: "viewer"}              │
│   ]                                              │
└─────────────────────────────────────────────────┘

Roles & Permissions:
┌─────────┬───────┬────────┬────────────────────┐
│ Role    │ Read  │ Write  │ Delete/Share       │
├─────────┼───────┼────────┼────────────────────┤
│ Owner   │ ✓     │ ✓      │ ✓                  │
│ Editor  │ ✓     │ ✓      │ ✗                  │
│ Viewer  │ ✓     │ ✗      │ ✗                  │
│ Public  │ ✓*    │ ✗      │ ✗ (*if is_public) │
└─────────┴───────┴────────┴────────────────────┘

Authorization checks:
├─ Extract user_id from JWT
├─ Load notebook + share_settings
├─ Determine user's role
├─ Check: does role allow action?
├─ If allowed → execute
└─ Else → 403 Forbidden
```

---

## Technology Choices: Why Matrix

```
┌────────────────────────────────────────────────────────────────────────┐
│ FASTAPI vs Django vs Flask                                            │
├────────────────────────────────────────────────────────────────────────┤
│ Choice: FastAPI                                                         │
│                                                                         │
│ Why?                                                                    │
│ ✅ Async/await native (we need high concurrency for LLM calls)        │
│ ✅ Modern: type hints, Pydantic validation, OpenAPI docs              │
│ ✅ Fast: ~45k req/sec benchmark                                       │
│ ✅ Smaller: lighter than Django for microservices                     │
│                                                                         │
│ Trade-off:                                                              │
│ ⚠️ Younger ecosystem (less Stack Overflow answers)                    │
│ ⚠️ ORM options less mature than Django                                │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ LANCEDB vs PINECONE vs WEAVIATE                                        │
├────────────────────────────────────────────────────────────────────────┤
│ Choice: LanceDB (self-hosted)                                           │
│                                                                         │
│ Why?                                                                    │
│ ✅ Self-hosted: no vendor lock-in, full control                       │
│ ✅ Built-in BM25: no need for separate Elasticsearch                  │
│ ✅ Fast: Arrow-columnar format, SIMD optimization                     │
│ ✅ Persistent: survives restarts (SSTable-based)                      │
│                                                                         │
│ Trade-off:                                                              │
│ ⚠️ Newer than competitors (less battle-tested)                        │
│ ⚠️ Smaller community (fewer tutorials)                                │
│ 🔮 Future: Could migrate to Pgvector (PostgreSQL) for multi-instance  │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ FASTEMBED vs OPENAI EMBEDDINGS vs SENTENCE-TRANSFORMERS                │
├────────────────────────────────────────────────────────────────────────┤
│ Choice: fastembed (ONNX-based, CPU)                                    │
│                                                                         │
│ Why?                                                                    │
│ ✅ Free: no API costs at scale (OpenAI costs $0.02/1M tokens)         │
│ ✅ Offline: no network calls, ultra-low latency                       │
│ ✅ Privacy: embeddings stay on your machine                           │
│ ✅ Fast: ONNX runtime is highly optimized                             │
│ ✅ Lightweight: 120MB model, works on laptops                         │
│                                                                         │
│ Trade-off:                                                              │
│ ⚠️ 384-dim vs 1536-dim (OpenAI): smaller, but sufficient for RAG       │
│ ⚠️ Not fine-tuned: generic model, could be better on domain data      │
│ ⚠️ Slower than GPU-based (but CPU is sufficient for batch processing)  │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ SQLITE vs POSTGRESQL vs MONGODB                                        │
├────────────────────────────────────────────────────────────────────────┤
│ Choice: SQLite (now)                                                    │
│                                                                         │
│ Why?                                                                    │
│ ✅ Zero ops: embedded in Python, single file                          │
│ ✅ ACID: guaranteed data safety                                       │
│ ✅ Simple: perfect for MVP                                            │
│ ✅ Portable: backup = copy file                                       │
│                                                                         │
│ Trade-off:                                                              │
│ ⚠️ Single-writer: concurrent writes can block                         │
│ ⚠️ Not networked: can't access from other machines                    │
│ 🔮 Future: PostgreSQL for multi-instance, replication                 │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ PYDANTIC vs DATACLASSES vs MARSHMALLOW                                  │
├────────────────────────────────────────────────────────────────────────┤
│ Choice: Pydantic                                                        │
│                                                                         │
│ Why?                                                                    │
│ ✅ Type-safe: runtime validation + IDE hints                          │
│ ✅ Serialization: to JSON, dict, etc.                                 │
│ ✅ Integration: native in FastAPI                                     │
│ ✅ Modern: follows Python 3.10+ standards                             │
│                                                                         │
│ Trade-off:                                                              │
│ ⚠️ Slight overhead: validation is slower than plain classes           │
│ ⚠️ Learning curve: complex for beginners                              │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Performance Optimization Techniques

```
┌────────────────────────────────────────────────────────┐
│ CACHING STRATEGY                                       │
├────────────────────────────────────────────────────────┤
│                                                         │
│ Level 1: In-Memory TTL Cache (cache.py)               │
│ ├─ TTL: 5 minutes (configurable)                      │
│ ├─ Keys: notebook:{id}, source:{id}, etc.            │
│ ├─ Benefits: Reduces DB hits by ~70%                 │
│ └─ Watch out: Stale cache in multi-instance setup    │
│                                                         │
│ Level 2: LanceDB Index Cache                          │
│ ├─ Lazy-loaded per notebook                          │
│ ├─ Indices: ANN + BM25                               │
│ ├─ Benefits: Fast retrieval after first query        │
│ └─ Watch out: Memory grows with # of notebooks       │
│                                                         │
│ Level 3: OS Filesystem Cache                          │
│ ├─ LanceDB files mmap'd into memory                  │
│ ├─ SQLite page cache (default 2000 pages)            │
│ ├─ Benefits: OS-level performance boost              │
│ └─ Trade-off: Competes with app memory               │
│                                                         │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│ ASYNC/AWAIT OPTIMIZATION                              │
├────────────────────────────────────────────────────────┤
│                                                         │
│ Ingestion Pipeline:                                    │
│ └─ Async background task (triggered by route)         │
│    ├─ Non-blocking: doesn't hold up HTTP response    │
│    ├─ User sees immediate: {status: "pending"}       │
│    └─ Can process 10+ uploads concurrently           │
│                                                         │
│ LLM Calls:                                             │
│ └─ Bottleneck: LLM API time (~2s per call)          │
│    ├─ Could parallelize: 6 concurrent summarizations │
│    ├─ via asyncio.gather([...])                      │
│    └─ (implemented in agent.py)                      │
│                                                         │
│ Retrieval:                                             │
│ └─ Fast: ~200-500ms                                  │
│    ├─ Dense search: 50-100ms (ANN index)             │
│    ├─ Sparse search: 20-50ms (BM25)                  │
│    ├─ Reranking: 50-100ms (cross-encoder)           │
│    └─ Total: ~200-250ms (acceptable)                │
│                                                         │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│ BATCHING OPTIMIZATION                                  │
├────────────────────────────────────────────────────────┤
│                                                         │
│ Embedding Batches:                                     │
│ └─ Process multiple chunks in parallel                │
│    ├─ Batch size: 16 (auto-tuned by hardware.py)    │
│    ├─ Speedup: ~8x vs single-chunk embedding        │
│    └─ Trade-off: Memory usage                        │
│                                                         │
│ Summarization Batches:                                 │
│ └─ Summarize 5-6 chunks at once (batch_size=800ch)  │
│    ├─ Respects context window: ~6000 chars           │
│    ├─ LLM processes multiple summaries concurrently  │
│    ├─ Concurrency: up to 6 parallel LLM calls       │
│    └─ Trade-off: Network overhead                    │
│                                                         │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│ HARDWARE-AWARE TUNING (hardware.py)                    │
├────────────────────────────────────────────────────────┤
│                                                         │
│ On Laptop (4 cores, 8GB RAM):                          │
│ ├─ EMBEDDING_THREADS = 2 (not 8)                     │
│ ├─ EMBEDDING_BATCH_SIZE = 8 (not 16)                │
│ ├─ SUMMARY_CONCURRENCY = 2 (not 6)                  │
│ └─ Result: No OOM, responsive UI                     │
│                                                         │
│ On Server (16 cores, 64GB RAM):                       │
│ ├─ EMBEDDING_THREADS = 8                            │
│ ├─ EMBEDDING_BATCH_SIZE = 32                        │
│ ├─ SUMMARY_CONCURRENCY = 16                         │
│ └─ Result: Maximum throughput                        │
│                                                         │
└────────────────────────────────────────────────────────┘
```

---

## Security Architecture Layers

```
┌────────────────────────────────────────────────────────────┐
│ LAYER 1: AUTHENTICATION                                    │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ OAuth 2.0 (GitHub, Google):                               │
│ ├─ User → clicks "Login with GitHub"                     │
│ ├─ Backend → exchanges code for token (server-to-server) │
│ ├─ Backend → fetches user info                           │
│ └─ Backend → issues JWT (HS256, 7-day expiry)           │
│                                                             │
│ Guest Sessions:                                            │
│ ├─ No OAuth needed                                        │
│ ├─ Browser → assigned cookie: guest_abc123               │
│ ├─ Cookie: Secure + HttpOnly (XSS-proof)                │
│ └─ Auto-cleanup: 30 days inactivity                      │
│                                                             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ LAYER 2: AUTHORIZATION                                     │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ Every Route:                                                │
│ 1. Extract user_id from JWT/cookie                        │
│ 2. Load resource (notebook)                               │
│ 3. Check: user_id == notebook.owner?                     │
│ 4. If yes → execute; if no → 403 Forbidden                │
│                                                             │
│ Notebook-Level:                                            │
│ ├─ Owner → Read/Write/Delete/Share                       │
│ ├─ Public reader → Read only (if is_public=true)        │
│ └─ Others → Denied                                        │
│                                                             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ LAYER 3: INPUT VALIDATION                                  │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ Pydantic Models:                                            │
│ ├─ All request bodies validated                           │
│ ├─ Type checking: int, str, list, etc.                   │
│ ├─ Range validation: max_length, min_value, etc.         │
│ └─ Custom validators: email format, URL format           │
│                                                             │
│ File Upload Validation:                                    │
│ ├─ File size limit (configurable, e.g., 100MB)           │
│ ├─ MIME type check (optional)                            │
│ └─ Virus scanning (not implemented yet)                   │
│                                                             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ LAYER 4: AUDIT & LOGGING                                   │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ Every Request Logged:                                      │
│ ├─ user_id, action, resource_type, resource_id           │
│ ├─ timestamp, ip_address, user_agent                     │
│ ├─ Request/response status                               │
│ └─ Stored in SQLite + rotating files                     │
│                                                             │
│ Usage:                                                      │
│ ├─ Compliance: GDPR/CCPA audit trail                     │
│ ├─ Debugging: trace user actions                         │
│ └─ Security: detect suspicious patterns                  │
│                                                             │
│ ⚠️ TODO: PII redaction (passwords, emails in logs)        │
│                                                             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ LAYER 5: DATA PROTECTION                                   │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ Data in Transit:                                            │
│ ├─ TLS 1.2+ recommended in production                     │
│ ├─ All sensitive data encrypted                          │
│ └─ No unencrypted HTTP in production                     │
│                                                             │
│ Data at Rest:                                              │
│ ├─ SQLite: unencrypted (⚠️ can add SQLCipher)            │
│ ├─ Uploads: unencrypted (segregated by guest_id)         │
│ ├─ Logs: unencrypted                                     │
│ └─ 🔒 Future: AES-256 at rest                            │
│                                                             │
│ Secrets Management:                                        │
│ ├─ AUTH_SECRET: environment variable                     │
│ ├─ OAuth credentials: environment variables              │
│ ├─ LLM API keys: environment variables                   │
│ ├─ ⚠️ TODO: Secret rotation policy                        │
│ └─ 🔒 Future: Vault integration                          │
│                                                             │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ KNOWN GAPS (To Address)                                    │
├────────────────────────────────────────────────────────────┤
│                                                             │
│ ❌ No rate limiting (abuse possible)                       │
│ ❌ No GDPR data export (compliance risk)                   │
│ ❌ No data encryption at rest                             │
│ ❌ No PII redaction in logs                               │
│ ❌ No session invalidation on logout                      │
│ ❌ JWT no refresh token (7-day re-login required)         │
│ ❌ No CORS origin validation (wide-open in dev)           │
│                                                             │
└────────────────────────────────────────────────────────────┘
```

---

**End of Visual Architecture Document**

Use these diagrams to quickly explain Calux Book's architecture to interviewers, team members, or stakeholders. Print or share digitally! 🎯
