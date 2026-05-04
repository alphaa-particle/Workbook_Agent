# Calux Book — Software Engineering Interview Dossier

## 1) Executive Summary

Calux Book is a privacy-first AI knowledge notebook built as a FastAPI backend with a lightweight single-page frontend. It allows users (authenticated or guest) to create notebooks, ingest sources (text/files/URLs), retrieve grounded context with vector + sparse search, and generate notes/chats from source-backed evidence.

From an interview perspective, this is a strong **MVP-to-production transition** system:
- **What it is**: a retrieval-augmented notebook platform.
- **How it works**: ingestion pipeline (parse/chunk/embed/index) + retrieval pipeline (hybrid search/reranking/context packing) + generation layer (provider abstraction).
- **Why this architecture**: fast iteration with practical performance and strong local/privacy defaults.

The major production-hardening priority is **authorization consistency** across notebook-scoped routes.

---

## 2) Product Goals and Non-Goals

### Goals
- Preserve user knowledge in notebook-centric structure.
- Support multimodal source ingestion with robust fallbacks.
- Generate grounded outputs rather than free-form hallucination.
- Keep operations simple using SQLite + LanceDB + filesystem uploads.
- Support guest mode for low-friction onboarding.

### Current Non-Goals (implied by implementation)
- Fully distributed ingestion workers.
- Strict enterprise-grade authz policy matrix out of the box.
- Frontend component modularization and test-heavy web architecture.

---

## 3) System Architecture (What)

## 3.1 Runtime Components
1. **API Gateway / Application Layer**
   - File: `calux_book/server.py`
   - FastAPI app factory, route handlers, startup/shutdown lifecycle, background ingestion scheduling.

2. **Authentication + Identity**
   - Files: `calux_book/auth.py`, `calux_book/middleware.py`
   - JWT generation/validation, origin utilities, guest identity extraction and cookie handling.

3. **Domain Storage (Transactional)**
   - File: `calux_book/store.py`
   - SQLite-backed CRUD for notebooks, sources, notes, chats, plus source fingerprinting and guest cleanup.

4. **Caching Layer**
   - File: `calux_book/cache.py`
   - TTL cache wrapper over store to reduce repetitive DB reads.

5. **Parsing and Content Normalization**
   - File: `calux_book/parser_router.py`
   - Parser routing for text/docx/csv/xlsx/pdf with OCR fallback policy.

6. **Embedding + Retrieval Layer**
   - Files: `calux_book/embedding.py`, `calux_book/vector_store.py`
   - Dense and optional sparse embedding, LanceDB indexing, hybrid retrieval and context packing.

7. **LLM Provider Abstraction + Agent Orchestration**
   - Files: `calux_book/providers.py`, `calux_book/agent.py`, `calux_book/prompts.py`
   - Provider interface to OpenAI/Gemini/GLM/ZImage; summarization/transformation/chat orchestration.

8. **Frontend SPA**
   - Files: `calux_book/frontend/index.html`, `calux_book/frontend/static/app.js`, `calux_book/frontend/static/style.css`
   - Notebook/source/note/chat workflows and polling for ingest status.

## 3.2 Operational Data Plane
- Uploaded files are stored under `data/uploads/...`.
- Structured app data is persisted via SQLite path from configuration.
- Vector chunks and metadata are stored in LanceDB at configured path.
- Audit logs are persisted using rotating log files.

---

## 4) Request Lifecycle (How)

1. Request enters FastAPI app and is observed by `AuditMiddleware`.
2. Route extracts user identity from JWT/cookie/query, with optional guest fallback.
3. Route-level service calls hit cached store first, then SQLite store.
4. Notebook-scoped AI operations may trigger lazy vector index loading for missing source fingerprints.
5. Agent executes retrieval → prompt construction → provider call → response shaping.
6. Route returns JSON/HTML/File responses, sometimes setting guest cookies to stabilize session identity.

### Why this flow?
- Keeps endpoints responsive by decoupling ingest work from request path.
- Reduces startup cost through lazy per-notebook index loading.
- Supports guest UX continuity with cookie restoration.

---

## 5) Data Lifecycle (How)

## 5.1 Ingestion Path
1. Source creation (`text`, `url`, `file`) writes a source record with state transitions (`pending`/`embedding`/`ready`/`error`).
2. Parser router extracts normalized text and metadata.
3. Text is chunked and embedded.
4. Chunks are written to LanceDB with source/notebook/page/section metadata.
5. Source status and chunk counts are updated in SQLite for frontend polling.

## 5.2 Retrieval + Generation Path
1. Agent receives user transformation or chat intent.
2. Vector store performs hybrid retrieval (dense + sparse + ranking logic).
3. Context packer assembles chunk evidence into prompt budget.
4. Provider adapter performs LLM completion.
5. Generated notes/chats and references are stored back in SQLite.

### Why this design?
- Separates concerns cleanly: storage, retrieval, generation.
- Enables provider/model swaps with minimal endpoint changes.
- Maintains traceability from output back to source chunks.

---

## 6) Module-by-Module Engineering Notes (What + Why)

## 6.1 `config.py`
- Centralized environment-driven settings for model providers, parser behavior, paths, and feature flags.
- Why: improves deployment portability and operational tunability.
- Interview note: config drift risk exists between sample env docs and runtime defaults if not contract-tested.

## 6.2 `hardware.py`
- Detects hardware profile and applies adaptive defaults.
- Why: avoid over-provisioning model operations on constrained systems.
- Interview note: strong practical optimization for laptop/edge environments.

## 6.3 `server.py`
- Monolithic route registration with lifecycle hooks and ingestion scheduler.
- Why: expedient MVP velocity and easy local reasoning.
- Interview note: good candidate for router/service decomposition as feature count grows.

## 6.4 `store.py` + `cache.py`
- Durable transactional model in SQLite, wrapped with lightweight TTL cache.
- Why: simple operational model and low infra burden.
- Interview note: cache invalidation strategy and stale-read behavior should be explicitly documented for multi-instance scaling.

## 6.5 `parser_router.py`
- Multi-format extraction with OCR fallback strategy.
- Why: real-world source heterogeneity demands layered parsing logic.
- Interview note: parser fallback ordering is a key reliability/control tradeoff.

## 6.6 `vector_store.py`
- Ingestion pipeline and hybrid retrieval/rerank/context assembly.
- Why: balances semantic matching with lexical recall and practical ranking.
- Interview note: retrieval quality depends heavily on chunk sizing, rerank policy, and source normalization.

## 6.7 `providers.py` + `agent.py` + `prompts.py`
- Provider abstraction and orchestration layer for transformations/chats.
- Why: vendor flexibility and reduced coupling to a single LLM API.
- Interview note: long-context strategies and output determinism controls are central production concerns.

## 6.8 Frontend (`app.js`)
- Single-file SPA orchestration for all user interactions.
- Why: minimal setup, fast iteration in early product stage.
- Interview note: maintainability risk rises with feature growth; split by feature modules before scale.

---

## 7) Security, Privacy, and Compliance Posture (What + Why)

### Strengths
- JWT-based authenticated access pattern exists.
- Guest identity is separated from authenticated identity namespace.
- Activity/audit logging middleware is present.
- Local embedding/retrieval options align with privacy-first operation.

### Critical Gaps (must-fix)
1. **Authorization consistency** on notebook-scoped endpoints is not uniformly enforced.
2. **Guest recovery permissiveness** may allow broader guest-to-guest notebook access than intended.

### Why this matters
- This is a direct data isolation risk, not only a code-style issue.
- In interview terms, this is the difference between “functional MVP” and “production-safe multi-tenant app.”

---

## 8) Validation Strategy and Evidence (Validate)

## 8.1 Existing Validation
- Test suite covers API behavior, auth helpers, cache/config/hardware/models/providers/store/vector store.
- Integration tests use FastAPI ASGI transport and mock model/parsing components for deterministic execution.

## 8.2 Validation Gaps
- Parser OCR fallback branch behavior under real malformed/scan PDFs.
- Middleware logging and identity extraction edge cases.
- Authorization negative tests for cross-user/cross-guest notebook access.
- Frontend workflow regressions (currently minimal/no browser test harness).

## 8.3 Full QA Strategy (Recommended)

### A) Unit and Integration
- Add explicit authz matrix tests for every notebook-scoped route.
- Add parser branch tests for each supported format and OCR fallback condition.
- Add agent behavior tests for map-reduce and token budget boundaries.

### B) Static and Type Safety
- Introduce `ruff` for linting and import hygiene.
- Introduce strict type checks (`mypy` or `pyright`) on backend package.

### C) Dependency and Security
- Add `pip-audit` or equivalent into CI.
- Add basic secret/config contract validation for mandatory env keys.

### D) Runtime and Reliability
- Add smoke tests for startup/shutdown, DB path availability, vector path readiness.
- Add timeout/retry/idempotency checks around background ingest workflows.

### E) Observability
- Define metrics for ingest latency, retrieval latency, chunk counts, and route-level errors.
- Add alerting thresholds for sustained source `error` state.

---

## 9) Production-Hardening Solution Roadmap (Provide a Solution)

## Priority 0 — Immediate (Security)
1. Centralize notebook ownership enforcement as a shared dependency/guard for all notebook-scoped routes.
2. Apply deny-by-default policy on route registration; explicit exceptions only.
3. Add negative-path tests for unauthorized access attempts.

## Priority 1 — Near-term (Reliability + Correctness)
1. Move fire-and-forget ingestion to a durable job queue abstraction (even if local first).
2. Add retry with idempotency keys for ingestion tasks.
3. Strengthen source status transitions and dead-letter/error reporting.

## Priority 2 — Mid-term (Scalability)
1. Optimize retrieval over large notebooks (index warmup, bounded retrieval windows, streaming context packing).
2. Reduce monolithic frontend surface by splitting feature modules.
3. Plan horizontal deployment model for store/cache/vector coordination.

## Priority 3 — Engineering Excellence
1. CI pipeline with lint/type/tests/security scanning.
2. Architecture decision records (ADR) for retrieval and authz policy.
3. SLOs for API latency, ingest success rate, and model response reliability.

---

## 10) Interview Talking Points

### Architecture
- Why SQLite + LanceDB is a pragmatic local-first choice.
- How hybrid retrieval improves relevance versus pure vector search.
- Why provider abstraction reduces vendor lock-in risk.

### Tradeoffs
- Fast MVP velocity vs. centralized policy enforcement.
- Guest convenience vs. strict tenant isolation.
- Monolithic frontend simplicity vs. long-term maintainability.

### Failure Modes
- Ingestion failures, parser fallbacks, model timeouts, stale cache, and auth mismatch.
- How each mode should degrade and be observed.

### Evolution Plan
- Security hardening first, then durability and scale.
- Preserve API contracts while refactoring internals incrementally.

---

## 11) Suggested Validation Commands

```bash
pytest -q
```

Recommended additions to CI:
```bash
ruff check .
mypy calux_book
pip-audit
```

---

## 12) Final Engineering Assessment

Calux Book is architecturally coherent and technically credible for a knowledge-notebook MVP with advanced retrieval capabilities. The codebase shows strong practical design choices (provider abstraction, lazy index loading, hardware-aware defaults, layered parser strategy). To reach production-grade readiness, the highest-impact next step is authorization hardening across all notebook-scoped operations, followed by durable ingestion execution and fuller QA automation.

If these priorities are addressed in order, the system is well-positioned to evolve from an effective prototype into a reliable multi-tenant product.
