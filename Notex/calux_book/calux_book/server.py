"""FastAPI server and route definitions for Calux Book.

Provides the full REST API matching the original Go backend's routing structure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent, create_agent
from .auth import AuthHandler, generate_jwt, get_origin_from_url
from .cache import CachedStore
from .config import Settings
from .middleware import (
    AuditMiddleware,
    extract_user_id,
    extract_user_id_optional,
    get_client_ip,
    log_user_activity,
    set_guest_cookie,
)
from .models import (
    ActivityLog,
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HealthResponse,
    Note,
    NotebookWithStats,
    Source,
    TransformationRequest,
)
from .store import Store
from .vector_store import VectorStore
from .hardware import get_hardware_profile, apply_hardware_defaults

logger = logging.getLogger("calux_book.server")

# Path to bundled frontend
_FRONTEND_DIR = Path(__file__).parent / "frontend"


def _sanitize_user_id(user_id: str) -> str:
    return user_id.replace(":", "_")


def _title_for_type(t: str) -> str:
    return {"summary": "Summary"}.get(t, "Note")


def _json_response_with_guest_cookie(payload: Any, user_id: str, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    set_guest_cookie(response, user_id)
    return response


class Server:
    """Encapsulates application state and builds the FastAPI instance."""

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.vector_store = VectorStore(cfg)
        self.store = Store(cfg.store_path)
        self.cached_store = CachedStore(self.store, ttl_seconds=300)
        self.agent: Agent | None = None
        self.auth: AuthHandler | None = None
        self._loaded_notebooks: set[str] = set()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        # Apply hardware-adaptive defaults before any model loading
        try:
            profile = get_hardware_profile()
            apply_hardware_defaults(self.cfg, profile)
        except Exception as e:
            logger.warning("Hardware detection failed, using defaults: %s", e)

        await self.store.initialize()
        self.agent = create_agent(self.cfg, self.vector_store)
        self.auth = AuthHandler(self.cfg, self.store)
        logger.info("Server initialized (vector index will load on demand)")

    async def load_notebook_vector_index(self, notebook_id: str) -> None:
        async with self._lock:
            if notebook_id in self._loaded_notebooks:
                return
            logger.info("Loading vector index for notebook %s", notebook_id)

            # ── Lightweight fingerprint check (no multi-MB content load) ──
            fp_rows = await self.store.list_source_fingerprints(notebook_id)

            needs_ingest_ids: list[str] = []
            for row in fp_rows:
                fp = row["content_hash"]
                if not fp:
                    # Legacy source without pre-computed hash — must
                    # fall back to full content load for this source only.
                    needs_ingest_ids.append(row["id"])
                    continue
                if fp in self.vector_store._ingested:
                    # Already in LanceDB — skip expensive re-chunking
                    continue
                needs_ingest_ids.append(row["id"])

            # Only load full content for sources that truly need ingestion
            if needs_ingest_ids:
                all_sources = await self.store.list_sources(notebook_id)
                src_map = {s.id: s for s in all_sources}
                for sid in needs_ingest_ids:
                    src = src_map.get(sid)
                    if not src or not src.content:
                        continue
                    # Backfill content_hash for legacy sources
                    if not src.content_hash:
                        src.content_hash = await self.store.backfill_content_hash(
                            src.id, notebook_id, src.name, src.content,
                        )
                        fp = src.content_hash
                        if fp in self.vector_store._ingested:
                            logger.info(
                                "Source '%s' hash backfilled — already in LanceDB, skipping",
                                src.name,
                            )
                            continue
                    logger.info("Ingesting new/changed source '%s'", src.name)
                    self.vector_store.ingest_source(
                        notebook_id, src.id, src.name, src.content,
                    )
            else:
                logger.info(
                    "All %d sources already ingested for notebook %s",
                    len(fp_rows), notebook_id,
                )

            self._loaded_notebooks.add(notebook_id)
            stats = self.vector_store.get_stats()
            logger.info(
                "Notebook %s ready (%d total docs, %d sources needed ingest)",
                notebook_id, stats.total_documents, len(needs_ingest_ids),
            )

    async def check_notebook_access(self, notebook_id: str, user_id: str) -> str | None:
        nb = await self.cached_store.get_notebook(notebook_id)
        if not nb:
            return "notebook not found"
        if nb.user_id and nb.user_id != user_id:
            # Recovery path for guest notebooks when browser guest identity changes
            # (e.g., stale cache, cookie cleared, or pre-cookie data created earlier).
            # Notebook IDs are UUIDs, so this keeps UX stable without affecting
            # authenticated user isolation.
            if nb.user_id.startswith("guest:") and user_id.startswith("guest:"):
                return None
            return "access denied"
        return None

    # ------------------------------------------------------------------
    # Background ingestion (keeps the API responsive)
    # ------------------------------------------------------------------

    def schedule_ingest(
        self,
        notebook_id: str,
        source_id: str,
        source_name: str,
        content: str,
    ) -> None:
        """Fire-and-forget ingestion in the running event loop.

        Parsing + embedding happen off the request path so the user gets
        an immediate 201 response.  Status is updated in the database at
        each stage so the frontend can poll for progress.
        """

        async def _bg_ingest() -> None:
            try:
                await self.cached_store.update_source_status(source_id, "embedding")
                loop = asyncio.get_running_loop()
                chunk_count = await loop.run_in_executor(
                    None,
                    self.vector_store.ingest_source,
                    notebook_id, source_id, source_name, content,
                )
                await self.cached_store.update_source_chunk_count(source_id, chunk_count)
                await self.cached_store.update_source_status(source_id, "ready")
                logger.info(
                    "Background ingest done: %s (%d chunks)", source_name, chunk_count,
                )
            except Exception as exc:
                logger.exception("Background ingest failed for %s", source_name)
                await self.cached_store.update_source_status(
                    source_id, "error", str(exc)[:500],
                )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_bg_ingest())
        except RuntimeError:
            # No event loop (e.g. CLI ingestion) — run synchronously
            import threading
            threading.Thread(
                target=lambda: self.vector_store.ingest_source(
                    notebook_id, source_id, source_name, content,
                ),
                daemon=True,
            ).start()


def create_app(cfg: Settings | None = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    if cfg is None:
        from .config import get_settings
        cfg = get_settings()

    app = FastAPI(title="Calux Book", version="1.0.0", docs_url=None, redoc_url=None)
    srv = Server(cfg)

    # -- Middleware ----------------------------------------------------------
    app.add_middleware(AuditMiddleware)

    # -- Lifespan -----------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        await srv.initialize()
        # Schedule periodic guest data cleanup
        if cfg.guest_expiry_days > 0:
            async def _guest_cleanup_loop() -> None:
                while True:
                    try:
                        await asyncio.sleep(86400)  # once per day
                        count = await srv.store.cleanup_expired_guests(cfg.guest_expiry_days)
                        if count:
                            logger.info("Guest cleanup removed %d notebooks", count)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.warning("Guest cleanup error: %s", e)
            asyncio.get_running_loop().create_task(_guest_cleanup_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await srv.store.close()

    # -- Static files -------------------------------------------------------
    static_dir = _FRONTEND_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # -- Frontend routes ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index():
        fp = _FRONTEND_DIR / "index.html"
        if fp.exists():
            return HTMLResponse(fp.read_text("utf-8"), headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<h1>Calux Book</h1>")

    @app.get("/notes/{nb_id}", response_class=HTMLResponse)
    async def notes_page(nb_id: str):
        fp = _FRONTEND_DIR / "index.html"
        if fp.exists():
            return HTMLResponse(fp.read_text("utf-8"), headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<h1>Calux Book</h1>")

    # ======================================================================
    # Public API routes (no auth) — MUST be registered before /public/{token}
    # ======================================================================

    @app.get("/public/notebooks")
    async def list_public_notebooks():
        notebooks = await srv.cached_store.list_public_notebooks()
        return JSONResponse([nb.model_dump(mode="json") for nb in notebooks])

    @app.get("/public/notebooks/{token}")
    async def get_public_notebook(token: str):
        nb = await srv.cached_store.get_notebook_by_public_token(token)
        if not nb:
            return JSONResponse({"error": "Public notebook not found"}, 404)
        return JSONResponse(nb.model_dump(mode="json"))

    @app.get("/public/notebooks/{token}/sources")
    async def list_public_sources(token: str):
        nb = await srv.cached_store.get_notebook_by_public_token(token)
        if not nb:
            return JSONResponse({"error": "Public notebook not found"}, 404)
        sources = await srv.cached_store.list_sources(nb.id)
        return JSONResponse([s.model_dump(mode="json") for s in sources])

    @app.get("/public/notebooks/{token}/notes")
    async def list_public_notes(token: str):
        nb = await srv.cached_store.get_notebook_by_public_token(token)
        if not nb:
            return JSONResponse({"error": "Public notebook not found"}, 404)
        notes = await srv.cached_store.list_notes(nb.id)
        return JSONResponse([n.model_dump(mode="json") for n in notes])

    # Catch-all for public notebook pages (SPA routing) — after specific routes
    @app.get("/public/{token}", response_class=HTMLResponse)
    async def public_page(token: str):
        fp = _FRONTEND_DIR / "index.html"
        if fp.exists():
            return HTMLResponse(fp.read_text("utf-8"), headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<h1>Calux Book</h1>")

    # ======================================================================
    # Auth routes
    # ======================================================================

    @app.get("/auth/login/{provider}")
    async def auth_login(provider: str):
        if provider == "github":
            url = await srv.auth.github_auth_url()
        elif provider == "google":
            url = await srv.auth.google_auth_url()
        else:
            return JSONResponse({"error": "Invalid provider"}, 400)
        return RedirectResponse(url)

    @app.get("/auth/callback/{provider}")
    async def auth_callback(provider: str, code: str = ""):
        if not code:
            return JSONResponse({"error": "Code not found"}, 400)
        try:
            if provider == "github":
                jwt_token, user = await srv.auth.github_callback(code)
                origin = get_origin_from_url(srv.cfg.github_redirect_url)
            elif provider == "google":
                jwt_token, user = await srv.auth.google_callback(code)
                origin = get_origin_from_url(srv.cfg.google_redirect_url)
            else:
                return JSONResponse({"error": "Invalid provider"}, 400)
        except Exception as e:
            logger.error("OAuth callback failed: %s", e)
            return JSONResponse({"error": str(e)}, 500)

        if not origin:
            origin = "http://localhost:8080"

        user_json = user.model_dump_json()
        html = (
            f"<script>"
            f'window.opener.postMessage({{token: "{jwt_token}", user: {user_json}}}, "{origin}");'
            f"window.close();</script>"
        )
        return HTMLResponse(html)

    # ======================================================================
    # File serving (with access control)
    # ======================================================================

    @app.get("/api/files/{filename}")
    async def serve_file(filename: str, request: Request):
        user_id = extract_user_id_optional(request, cfg.jwt_secret)
        if not filename:
            return JSONResponse({"error": "filename required"}, 400)

        owner_user_id = ""
        is_public = False

        result = await srv.cached_store.get_source_by_filename(filename)
        if result:
            source, notebook = result
            owner_user_id = notebook.user_id
            is_public = notebook.is_public
        else:
            result2 = await srv.cached_store.get_note_by_filename(filename)
            if result2:
                note, notebook = result2
                owner_user_id = notebook.user_id
                is_public = notebook.is_public
            else:
                return JSONResponse({"error": "File not found"}, 404)

        if not is_public:
            if not user_id:
                return JSONResponse({"error": "Authorization required"}, 401)
            if user_id != owner_user_id:
                return JSONResponse({"error": "Access denied"}, 403)

        file_path = Path("./data/uploads") / _sanitize_user_id(owner_user_id) / filename
        abs_path = file_path.resolve()
        abs_upload = Path("./data/uploads").resolve()
        if not str(abs_path).startswith(str(abs_upload)):
            return JSONResponse({"error": "Access denied"}, 403)
        if not abs_path.exists():
            return JSONResponse({"error": "File not found"}, 404)

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        cache = "public, max-age=3600" if is_public else "no-cache"
        return FileResponse(str(abs_path), media_type=content_type, headers={"Cache-Control": cache})

    # ======================================================================
    # API routes â€” Health
    # ======================================================================

    @app.get("/api/health")
    async def health(request: Request):
        uid = extract_user_id(request, cfg.jwt_secret)
        llm_model = cfg.openai_model
        if cfg.is_ollama:
            llm_model = f"{cfg.ollama_model} (Ollama)"
        return HealthResponse(
            status="ok", version="1.0.0", timestamp=int(time.time()),
            services={"vector_store": "lancedb", "llm": llm_model},
        )

    @app.get("/api/config")
    async def get_config(request: Request):
        extract_user_id(request, cfg.jwt_secret)
        return {}

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, 401)
        user = await srv.cached_store.get_user(user_id)
        if not user:
            return JSONResponse({"error": "User not found"}, 404)
        return user.model_dump(mode="json")

    # ======================================================================
    # API routes â€” Notebooks
    # ======================================================================

    @app.get("/api/notebooks")
    async def list_notebooks(request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        notebooks = await srv.cached_store.list_notebooks(user_id)
        return _json_response_with_guest_cookie(
            [nb.model_dump(mode="json") for nb in notebooks],
            user_id,
        )

    @app.get("/api/notebooks/stats")
    async def list_notebooks_with_stats(request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        if not user_id:
            return JSONResponse([])
        notebooks = await srv.cached_store.list_notebooks_with_stats(user_id)
        return JSONResponse([nb.model_dump(mode="json") for nb in notebooks])

    @app.post("/api/notebooks", status_code=201)
    async def create_notebook(request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        body = await request.json()
        name = body.get("name", "")
        if not name:
            return JSONResponse({"error": "name is required"}, 400)
        nb = await srv.cached_store.create_notebook(
            user_id, name, body.get("description", ""), body.get("metadata"),
        )
        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action="create_notebook", resource_type="notebook",
            resource_id=nb.id, resource_name=nb.name,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))
        return _json_response_with_guest_cookie(
            nb.model_dump(mode="json"),
            user_id,
            status_code=201,
        )

    @app.get("/api/notebooks/{nb_id}")
    async def get_notebook(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        nb = await srv.cached_store.get_notebook(nb_id)
        if not nb:
            return JSONResponse({"error": "Notebook not found"}, 404)
        if nb.user_id and nb.user_id != user_id:
            return JSONResponse({"error": "Access denied"}, 403)
        return JSONResponse(nb.model_dump(mode="json"))

    @app.put("/api/notebooks/{nb_id}")
    async def update_notebook(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        existing = await srv.cached_store.get_notebook(nb_id)
        if not existing:
            return JSONResponse({"error": "Notebook not found"}, 404)
        if existing.user_id and existing.user_id != user_id:
            return JSONResponse({"error": "Access denied"}, 403)
        body = await request.json()
        nb = await srv.cached_store.update_notebook(
            nb_id, body.get("name", ""), body.get("description", ""), body.get("metadata"),
        )
        return JSONResponse(nb.model_dump(mode="json"))

    @app.delete("/api/notebooks/{nb_id}", status_code=204)
    async def delete_notebook(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        existing = await srv.cached_store.get_notebook(nb_id)
        if not existing:
            return JSONResponse({"error": "Notebook not found"}, 404)
        if existing.user_id and existing.user_id != user_id:
            return JSONResponse({"error": "Access denied"}, 403)
        await srv.cached_store.delete_notebook(nb_id)
        return Response(status_code=204)

    @app.put("/api/notebooks/{nb_id}/public")
    async def set_notebook_public(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        existing = await srv.cached_store.get_notebook(nb_id)
        if not existing:
            return JSONResponse({"error": "Notebook not found"}, 404)
        if existing.user_id and existing.user_id != user_id:
            return JSONResponse({"error": "Access denied"}, 403)
        body = await request.json()
        nb = await srv.cached_store.set_notebook_public(nb_id, body.get("is_public", False))
        action = "make_public" if body.get("is_public") else "make_private"
        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action=action, resource_type="notebook",
            resource_id=nb.id, resource_name=nb.name,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))
        return JSONResponse(nb.model_dump(mode="json"))

    # ======================================================================
    # API routes â€” Sources
    # ======================================================================

    @app.get("/api/notebooks/{nb_id}/sources")
    async def list_sources(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        err = await srv.check_notebook_access(nb_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)
        sources = await srv.cached_store.list_sources(nb_id)
        return JSONResponse([s.model_dump(mode="json") for s in sources])

    @app.post("/api/notebooks/{nb_id}/sources", status_code=201)
    async def add_source(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        err = await srv.check_notebook_access(nb_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)
        body = await request.json()
        source = Source(
            notebook_id=nb_id,
            name=body.get("name", ""),
            type=body.get("type", ""),
            url=body.get("url", ""),
            content=body.get("content", ""),
            status="ready" if body.get("content") else "pending",
            metadata=body.get("metadata", {}),
        )
        if source.url and not source.content:
            try:
                source.content = srv.vector_store.extract_from_url(source.url)
                source.status = "ready"
            except Exception as e:
                return JSONResponse({"error": f"Failed to fetch URL: {e}"}, 500)
        source = await srv.cached_store.create_source(source)
        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action="add_source", resource_type="source",
            resource_id=source.id, resource_name=source.name,
            details=json.dumps({"notebook_id": nb_id, "source_type": source.type}),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))
        if source.content:
            srv.schedule_ingest(nb_id, source.id, source.name, source.content)
        return JSONResponse(source.model_dump(mode="json"), 201)

    @app.delete("/api/notebooks/{nb_id}/sources/{source_id}", status_code=204)
    async def delete_source(nb_id: str, source_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        source = await srv.cached_store.get_source(source_id)
        if not source:
            return JSONResponse({"error": "Source not found"}, 404)
        err = await srv.check_notebook_access(source.notebook_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)
        await srv.cached_store.delete_source(source_id)
        srv.vector_store.delete(source.notebook_id, source.id, source.name)
        return Response(status_code=204)

    @app.get("/api/notebooks/{nb_id}/sources/status")
    async def sources_status(nb_id: str, request: Request):
        """Return lightweight status info for all sources in a notebook.

        Frontend polls this to update processing indicators without
        fetching full content.
        """
        user_id = extract_user_id(request, cfg.jwt_secret)
        err = await srv.check_notebook_access(nb_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)
        sources = await srv.cached_store.list_sources(nb_id)
        status_list = [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "chunk_count": s.chunk_count,
                "error_message": s.error_message,
            }
            for s in sources
        ]
        return JSONResponse(status_list)
    @app.get("/api/notebooks/{nb_id}/sources/{source_id}/pages")
    async def get_source_pages(nb_id: str, source_id: str, request: Request):
        """Return the page index for a specific source."""
        user_id = extract_user_id(request, cfg.jwt_secret)
        err = await srv.check_notebook_access(nb_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)
        pages = await srv.store.get_page_index(source_id)
        return JSONResponse(pages)
    # ======================================================================
    # API routes â€” Upload
    # ======================================================================

    @app.post("/api/upload", status_code=201)
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        notebook_id: str = Form(...),
    ):
        user_id = extract_user_id(request, cfg.jwt_secret)
        err = await srv.check_notebook_access(notebook_id, user_id)
        if err:
            return JSONResponse({"error": err}, 403)

        ext = os.path.splitext(file.filename or "")[1]
        base = (file.filename or "upload")
        if ext:
            base = base[: -len(ext)]
        unique_name = f"{base}_{uuid4().hex[:8]}{ext}"
        safe_uid = _sanitize_user_id(user_id)
        upload_dir = Path(f"./data/uploads/{safe_uid}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        temp_path = upload_dir / unique_name

        data = await file.read()
        temp_path.write_bytes(data)

        source = Source(
            notebook_id=notebook_id,
            name=file.filename or unique_name,
            type="file",
            file_name=unique_name,
            file_size=len(data),
            status="pending",
            metadata={"path": str(temp_path), "user_id": user_id},
        )

        # Extraction + ingestion run in the background so the user
        # gets an immediate 201 and sees a progress indicator in the UI.
        source = await srv.cached_store.create_source(source)

        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action="upload_file", resource_type="source",
            resource_id=source.id, resource_name=file.filename or unique_name,
            details=json.dumps({"notebook_id": notebook_id, "file_size": len(data)}),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))

        async def _bg_extract_and_ingest() -> None:
            """Background: extract text → embed → store in LanceDB."""
            try:
                await srv.cached_store.update_source_status(source.id, "extracting")
                loop = asyncio.get_running_loop()

                # Progress callback that updates DB status
                def _progress(stage: str, detail: str, percent: int) -> None:
                    # Fire-and-forget status update from sync context
                    try:
                        loop.call_soon_threadsafe(
                            loop.create_task,
                            srv.cached_store.update_source_status(source.id, stage, detail),
                        )
                    except Exception:
                        pass

                content = await loop.run_in_executor(
                    None,
                    lambda: srv.vector_store.extract_document(
                        str(temp_path), progress_cb=_progress,
                    ),
                )
                if content:
                    # Persist extracted text in the metadata store so it is
                    # visible when the user lists sources.
                    await srv.cached_store.update_source_content(
                        source.id, content,
                        notebook_id=notebook_id, name=source.name,
                    )
                    await srv.cached_store.update_source_status(source.id, "embedding")
                    chunk_count = await loop.run_in_executor(
                        None,
                        srv.vector_store.ingest_source,
                        notebook_id, source.id, source.name, content,
                    )
                    await srv.cached_store.update_source_chunk_count(source.id, chunk_count)

                    # Populate page index from the already-extracted content
                    # string (uses [PAGE N] markers) — no need to re-parse
                    # the PDF file from disk.
                    try:
                        page_chunks = srv.vector_store._split_into_page_chunks(
                            content, cfg.chunk_size, cfg.chunk_overlap,
                            source_name=source.name,
                        )
                        # Group chunks by page to compute per-page stats
                        from collections import defaultdict
                        page_groups: dict[int, list[dict]] = defaultdict(list)
                        for pc in page_chunks:
                            page_groups[pc["page_number"]].append(pc)

                        # Track sections for section_index
                        section_tracker: dict[str, dict] = {}  # section_path → {start, end, title, depth}

                        chunk_idx = 0
                        for pg_num in sorted(page_groups.keys()):
                            pg_group = page_groups[pg_num]
                            pg_chunk_count = len(pg_group)
                            snippet = pg_group[0]["text"][:200].strip() if pg_group else ""
                            section_path = pg_group[0].get("section_path", "") if pg_group else ""

                            await srv.store.upsert_page_index(
                                notebook_id, source.id, pg_num,
                                pg_chunk_count, chunk_idx, snippet,
                                section_path=section_path,
                            )

                            # Track section spans
                            if section_path:
                                if section_path not in section_tracker:
                                    title = pg_group[0].get("section_title", "")
                                    depth = section_path.count(" > ") + 1
                                    section_tracker[section_path] = {
                                        "title": title,
                                        "start": pg_num,
                                        "end": pg_num,
                                        "depth": depth,
                                    }
                                else:
                                    section_tracker[section_path]["end"] = pg_num

                            chunk_idx += pg_chunk_count

                        # Populate section_index
                        for spath, info in section_tracker.items():
                            try:
                                await srv.store.upsert_section_index(
                                    source.id,
                                    info["title"],
                                    spath,
                                    info["start"],
                                    info["end"],
                                    info["depth"],
                                )
                            except Exception:
                                pass

                        pages = list(page_groups.keys())
                    except Exception as pi_err:
                        logger.debug("Page index population skipped: %s", pi_err)
                        pages = []

                    await srv.cached_store.update_source_status(source.id, "ready")
                    logger.info(
                        "Upload ingest done: %s (%d chunks, %d pages)",
                        source.name, chunk_count,
                        len(pages) if pages else 0,
                    )
                else:
                    await srv.cached_store.update_source_status(
                        source.id, "error", "No text could be extracted",
                    )
            except Exception as exc:
                logger.exception("Background upload ingest failed for %s", source.name)
                await srv.cached_store.update_source_status(
                    source.id, "error", str(exc)[:500],
                )

        try:
            asyncio.get_running_loop().create_task(_bg_extract_and_ingest())
        except RuntimeError:
            pass  # fallback: will be ingested on next notebook load

        return JSONResponse(source.model_dump(mode="json"), 201)

    # ======================================================================
    # API routes â€” Notes
    # ======================================================================

    @app.get("/api/notebooks/{nb_id}/notes")
    async def list_notes(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        notes = await srv.cached_store.list_notes(nb_id)
        return JSONResponse([n.model_dump(mode="json") for n in notes])

    @app.post("/api/notebooks/{nb_id}/notes", status_code=201)
    async def create_note(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        body = await request.json()
        note = Note(
            notebook_id=nb_id,
            title=body.get("title", ""),
            content=body.get("content", ""),
            type=body.get("type", ""),
            source_ids=body.get("source_ids", []),
        )
        note = await srv.cached_store.create_note(note)
        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action="create_note", resource_type="note",
            resource_id=note.id, resource_name=note.title,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))
        return JSONResponse(note.model_dump(mode="json"), 201)

    @app.delete("/api/notebooks/{nb_id}/notes/{note_id}", status_code=204)
    async def delete_note(nb_id: str, note_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        await srv.cached_store.delete_note(note_id)
        return Response(status_code=204)

    # ======================================================================
    # API routes â€” Transform
    # ======================================================================

    @app.post("/api/notebooks/{nb_id}/transform")
    async def transform(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        await srv.load_notebook_vector_index(nb_id)
        body = await request.json()
        req = TransformationRequest(**body)

        if not cfg.allow_multiple_notes_of_same_type:
            existing = await srv.cached_store.list_notes(nb_id)
            for n in existing:
                if n.type == req.type:
                    return JSONResponse(
                        {"error": "A note of this type already exists"}, 409,
                    )

        sources = await srv.cached_store.list_sources(nb_id)
        if req.source_ids:
            sid_set = set(req.source_ids)
            sources = [s for s in sources if s.id in sid_set]
        else:
            req.source_ids = [s.id for s in sources]

        if not sources:
            return JSONResponse({"error": "No sources available"}, 400)

        # Wrap in a timeout to prevent very large documents from
        # blocking the server indefinitely during map-reduce.
        timeout_secs = getattr(cfg, "summary_timeout", 600)
        try:
            resp = await asyncio.wait_for(
                srv.agent.generate_transformation(req, sources),
                timeout=timeout_secs,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Transform timed out after %ds for notebook %s",
                timeout_secs, nb_id,
            )
            return JSONResponse(
                {"error": f"Generation timed out after {timeout_secs}s. "
                 "Try a shorter summary or fewer sources."},
                504,
            )

        note = Note(
            notebook_id=nb_id,
            title=_title_for_type(req.type),
            content=resp.content,
            type=req.type,
            source_ids=req.source_ids,
            metadata={"length": req.length, "format": req.format},
        )
        note = await srv.cached_store.create_note(note)

        await srv.cached_store.log_activity(ActivityLog(
            user_id=user_id, action="transform", resource_type="note",
            resource_id=note.id, resource_name=note.title,
            details=json.dumps({"notebook_id": nb_id, "type": req.type}),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        ))
        return JSONResponse(note.model_dump(mode="json"))

    # ======================================================================
    # API routes â€” Chat
    # ======================================================================

    @app.get("/api/notebooks/{nb_id}/chat/sessions")
    async def list_chat_sessions(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        sessions = await srv.cached_store.list_chat_sessions(nb_id)
        return JSONResponse([s.model_dump(mode="json") for s in sessions])

    @app.post("/api/notebooks/{nb_id}/chat/sessions", status_code=201)
    async def create_chat_session(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        body = await request.json()
        session = await srv.cached_store.create_chat_session(nb_id, body.get("title", ""))
        return JSONResponse(session.model_dump(mode="json"), 201)

    @app.delete("/api/notebooks/{nb_id}/chat/sessions/{session_id}", status_code=204)
    async def delete_chat_session(nb_id: str, session_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        await srv.cached_store.delete_chat_session(session_id)
        return Response(status_code=204)

    @app.post("/api/notebooks/{nb_id}/chat/sessions/{session_id}/messages")
    async def send_message(nb_id: str, session_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        await srv.load_notebook_vector_index(nb_id)
        body = await request.json()
        message = body.get("message", "")

        await srv.cached_store.add_chat_message(session_id, "user", message)
        session = await srv.cached_store.get_chat_session(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, 404)

        resp = await srv.agent.chat(nb_id, message, session.messages)

        source_ids = [s.id for s in resp.sources]
        await srv.cached_store.add_chat_message(session_id, "assistant", resp.message, source_ids)
        return JSONResponse(resp.model_dump(mode="json"))

    @app.post("/api/notebooks/{nb_id}/chat")
    async def quick_chat(nb_id: str, request: Request):
        user_id = extract_user_id(request, cfg.jwt_secret)
        await srv.load_notebook_vector_index(nb_id)
        body = await request.json()
        chat_req = ChatRequest(**body)

        session_id = chat_req.session_id
        if not session_id:
            session = await srv.cached_store.create_chat_session(nb_id)
            session_id = session.id

        session = await srv.cached_store.get_chat_session(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, 404)

        resp = await srv.agent.chat(nb_id, chat_req.message, session.messages)
        resp.session_id = session_id

        source_ids = [s.id for s in resp.sources]
        await srv.cached_store.add_chat_message(session_id, "user", chat_req.message)
        await srv.cached_store.add_chat_message(session_id, "assistant", resp.message, source_ids)
        return JSONResponse(resp.model_dump(mode="json"))

    return app
