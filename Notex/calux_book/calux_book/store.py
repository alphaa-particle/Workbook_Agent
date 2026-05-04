"""SQLite persistence store for Calux Book.

Provides async CRUD operations for notebooks, sources, notes, chat sessions,
users, and activity logs using aiosqlite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from .models import (
    ActivityLog,
    ChatMessage,
    ChatSession,
    Note,
    Notebook,
    NotebookWithStats,
    Source,
    User,
)

logger = logging.getLogger("calux_book.store")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    avatar_url TEXT,
    provider TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    is_public INTEGER DEFAULT 0,
    public_token TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT,
    content TEXT,
    file_name TEXT,
    file_size INTEGER,
    chunk_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    error_message TEXT DEFAULT '',
    content_hash TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata TEXT,
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    type TEXT NOT NULL,
    source_ids TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata TEXT,
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    metadata TEXT,
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources TEXT,
    created_at INTEGER NOT NULL,
    metadata TEXT,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS activity_logs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    resource_name TEXT,
    details TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_sources_notebook ON sources(notebook_id);
CREATE INDEX IF NOT EXISTS idx_notes_notebook ON notes(notebook_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_notebook ON chat_sessions(notebook_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_activity_logs_user ON activity_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_activity_logs_created ON activity_logs(created_at);

CREATE TABLE IF NOT EXISTS page_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL DEFAULT 1,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    first_chunk_idx INTEGER NOT NULL DEFAULT 0,
    summary_snippet TEXT DEFAULT '',
    section_path TEXT DEFAULT '',
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_page_index_source ON page_index(source_id);
CREATE INDEX IF NOT EXISTS idx_page_index_notebook ON page_index(notebook_id);

CREATE TABLE IF NOT EXISTS section_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    section_title TEXT NOT NULL,
    section_path TEXT NOT NULL DEFAULT '',
    start_page INTEGER NOT NULL DEFAULT 1,
    end_page INTEGER NOT NULL DEFAULT 1,
    depth INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_section_index_source ON section_index(source_id);
"""


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_ts(ts: int) -> datetime:
    return datetime.utcfromtimestamp(ts)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str) if obj else "{}"


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Async SQLite data store."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        # Run lightweight migrations for new columns
        await self._migrate()
        logger.info("Store initialized at %s", os.path.abspath(self.db_path))

    async def _migrate(self) -> None:
        """Add columns introduced after initial schema."""
        migrations = [
            ("sources", "status", "ALTER TABLE sources ADD COLUMN status TEXT DEFAULT 'pending'"),
            ("sources", "error_message", "ALTER TABLE sources ADD COLUMN error_message TEXT DEFAULT ''"),
            ("sources", "content_hash", "ALTER TABLE sources ADD COLUMN content_hash TEXT DEFAULT ''"),
            ("page_index", "section_path", "ALTER TABLE page_index ADD COLUMN section_path TEXT DEFAULT ''"),
        ]
        for table, column, sql in migrations:
            try:
                cur = await self.db.execute(f"PRAGMA table_info({table})")
                cols = {r[1] for r in await cur.fetchall()}
                if column not in cols:
                    await self.db.execute(sql)
                    await self.db.commit()
                    logger.info("Migration: added %s.%s", table, column)
            except Exception as e:
                logger.debug("Migration skip %s.%s: %s", table, column, e)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not initialized"
        return self._db

    # -- helpers --------------------------------------------------------------

    async def _ensure_user_exists(self, user_id: str) -> None:
        if not user_id:
            return
        row = await self.db.execute_fetchall(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        )
        if row:
            return
        now = _ts(datetime.utcnow())
        safe = user_id.replace(":", "_").replace(" ", "_").replace("@", "_at_")
        email = f"{safe}@local.calux_book"
        await self.db.execute(
            "INSERT OR IGNORE INTO users (id, email, name, avatar_url, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, email, user_id, "", "guest", now, now),
        )
        await self.db.commit()

    # ======================================================================
    # User operations
    # ======================================================================

    async def create_user(self, user: User) -> None:
        now = datetime.utcnow()
        if not user.created_at or user.created_at == datetime.min:
            user.created_at = now
        user.updated_at = now

        existing = await self.get_user_by_email(user.email)
        if existing:
            user.id = existing.id
            user.created_at = existing.created_at
            await self.db.execute(
                "UPDATE users SET name=?, avatar_url=?, provider=?, updated_at=? WHERE id=?",
                (user.name, user.avatar_url, user.provider, _ts(now), user.id),
            )
            await self.db.commit()
            return

        if not user.id:
            user.id = str(uuid4())
        await self.db.execute(
            "INSERT INTO users (id, email, name, avatar_url, provider, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.id, user.email, user.name, user.avatar_url, user.provider,
             _ts(user.created_at), _ts(user.updated_at)),
        )
        await self.db.commit()

    async def get_user(self, user_id: str) -> User | None:
        cur = await self.db.execute(
            "SELECT id, email, name, avatar_url, provider, created_at, updated_at "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return User(
            id=row[0], email=row[1], name=row[2], avatar_url=row[3],
            provider=row[4], created_at=_from_ts(row[5]), updated_at=_from_ts(row[6]),
        )

    async def get_user_by_email(self, email: str) -> User | None:
        cur = await self.db.execute(
            "SELECT id, email, name, avatar_url, provider, created_at, updated_at "
            "FROM users WHERE email = ?",
            (email,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return User(
            id=row[0], email=row[1], name=row[2], avatar_url=row[3],
            provider=row[4], created_at=_from_ts(row[5]), updated_at=_from_ts(row[6]),
        )

    # ======================================================================
    # Notebook operations
    # ======================================================================

    async def create_notebook(
        self, user_id: str, name: str, description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Notebook:
        await self._ensure_user_exists(user_id)
        nb_id = str(uuid4())
        now = datetime.utcnow()
        await self.db.execute(
            "INSERT INTO notebooks (id, user_id, name, description, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nb_id, user_id, name, description, _ts(now), _ts(now), _json_dumps(metadata)),
        )
        await self.db.commit()
        return (await self.get_notebook(nb_id))  # type: ignore[return-value]

    async def get_notebook(self, nb_id: str) -> Notebook | None:
        cur = await self.db.execute(
            "SELECT id, user_id, name, description, is_public, public_token, "
            "created_at, updated_at, metadata FROM notebooks WHERE id = ?",
            (nb_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Notebook(
            id=row[0], user_id=row[1] or "", name=row[2], description=row[3] or "",
            is_public=bool(row[4]), public_token=row[5] or "",
            created_at=_from_ts(row[6]), updated_at=_from_ts(row[7]),
            metadata=_json_loads(row[8]),
        )

    async def list_notebooks(self, user_id: str) -> list[Notebook]:
        cur = await self.db.execute(
            "SELECT id, user_id, name, description, is_public, public_token, "
            "created_at, updated_at, metadata FROM notebooks WHERE user_id = ? "
            "ORDER BY updated_at DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [
            Notebook(
                id=r[0], user_id=r[1] or "", name=r[2], description=r[3] or "",
                is_public=bool(r[4]), public_token=r[5] or "",
                created_at=_from_ts(r[6]), updated_at=_from_ts(r[7]),
                metadata=_json_loads(r[8]),
            )
            for r in rows
        ]

    async def list_notebooks_with_stats(self, user_id: str) -> list[NotebookWithStats]:
        cur = await self.db.execute(
            "SELECT n.id, n.user_id, n.name, n.description, n.is_public, n.public_token, "
            "n.created_at, n.updated_at, n.metadata, "
            "COALESCE((SELECT COUNT(*) FROM sources WHERE notebook_id = n.id), 0), "
            "COALESCE((SELECT COUNT(*) FROM notes WHERE notebook_id = n.id), 0) "
            "FROM notebooks n WHERE n.user_id = ? ORDER BY n.updated_at DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [
            NotebookWithStats(
                id=r[0], user_id=r[1] or "", name=r[2], description=r[3] or "",
                is_public=bool(r[4]), public_token=r[5] or "",
                created_at=_from_ts(r[6]), updated_at=_from_ts(r[7]),
                metadata=_json_loads(r[8]), source_count=r[9], note_count=r[10],
            )
            for r in rows
        ]

    async def update_notebook(
        self, nb_id: str, name: str, description: str,
        metadata: dict[str, Any] | None = None,
    ) -> Notebook | None:
        now = datetime.utcnow()
        await self.db.execute(
            "UPDATE notebooks SET name=?, description=?, updated_at=?, metadata=? WHERE id=?",
            (name, description, _ts(now), _json_dumps(metadata), nb_id),
        )
        await self.db.commit()
        return await self.get_notebook(nb_id)

    async def delete_notebook(self, nb_id: str) -> None:
        await self.db.execute("DELETE FROM notebooks WHERE id = ?", (nb_id,))
        await self.db.commit()

    async def set_notebook_public(self, nb_id: str, is_public: bool) -> Notebook | None:
        now = _ts(datetime.utcnow())
        if is_public:
            token = str(uuid4())
            await self.db.execute(
                "UPDATE notebooks SET is_public=1, public_token=?, updated_at=? WHERE id=?",
                (token, now, nb_id),
            )
        else:
            await self.db.execute(
                "UPDATE notebooks SET is_public=0, public_token=NULL, updated_at=? WHERE id=?",
                (now, nb_id),
            )
        await self.db.commit()
        return await self.get_notebook(nb_id)

    async def get_notebook_by_public_token(self, token: str) -> Notebook | None:
        cur = await self.db.execute(
            "SELECT id, user_id, name, description, is_public, public_token, "
            "created_at, updated_at, metadata FROM notebooks "
            "WHERE public_token = ? AND is_public = 1",
            (token,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Notebook(
            id=row[0], user_id=row[1] or "", name=row[2], description=row[3] or "",
            is_public=bool(row[4]), public_token=row[5] or "",
            created_at=_from_ts(row[6]), updated_at=_from_ts(row[7]),
            metadata=_json_loads(row[8]),
        )

    async def list_public_notebooks(self) -> list[NotebookWithStats]:
        cur = await self.db.execute(
            "SELECT DISTINCT n.id, n.user_id, n.name, n.description, n.is_public, n.public_token, "
            "n.created_at, n.updated_at, n.metadata, "
            "COALESCE((SELECT COUNT(*) FROM sources WHERE notebook_id = n.id), 0), "
            "COALESCE((SELECT COUNT(*) FROM notes WHERE notebook_id = n.id), 0) "
            "FROM notebooks n WHERE n.is_public = 1 ORDER BY n.updated_at DESC LIMIT 20",
        )
        rows = await cur.fetchall()
        return [
            NotebookWithStats(
                id=r[0], user_id=r[1] or "", name=r[2], description=r[3] or "",
                is_public=bool(r[4]), public_token=r[5] or "",
                created_at=_from_ts(r[6]), updated_at=_from_ts(r[7]),
                metadata=_json_loads(r[8]), source_count=r[9], note_count=r[10],
            )
            for r in rows
        ]

    # ======================================================================
    # Source operations
    # ======================================================================

    async def create_source(self, source: Source) -> Source:
        source.id = str(uuid4())
        now = datetime.utcnow()
        source.created_at = now
        source.updated_at = now
        # Pre-compute content fingerprint so we never need to reload
        # full content just for dedup checks.
        if source.content and not source.content_hash:
            source.content_hash = self._compute_content_hash(
                source.notebook_id, source.id, source.name, source.content,
            )
        await self.db.execute(
            "INSERT INTO sources (id, notebook_id, name, type, url, content, file_name, "
            "file_size, chunk_count, status, error_message, content_hash, "
            "created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source.id, source.notebook_id, source.name, source.type, source.url,
             source.content, source.file_name, source.file_size, source.chunk_count,
             source.status, source.error_message, source.content_hash,
             _ts(now), _ts(now), _json_dumps(source.metadata)),
        )
        await self.db.commit()
        return source

    @staticmethod
    def _compute_content_hash(
        notebook_id: str, source_id: str, source_name: str, content: str,
    ) -> str:
        """SHA-256 of notebook+source+content — matches VectorStore._fingerprint."""
        raw = f"{notebook_id}\n{source_id}\n{source_name}\n{content}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get_source(self, source_id: str) -> Source | None:
        cur = await self.db.execute(
            "SELECT id, notebook_id, name, type, url, content, file_name, file_size, "
            "chunk_count, created_at, updated_at, metadata, "
            "COALESCE(status, 'pending'), COALESCE(error_message, '') "
            "FROM sources WHERE id = ?",
            (source_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return Source(
            id=row[0], notebook_id=row[1], name=row[2], type=row[3],
            url=row[4] or "", content=row[5] or "", file_name=row[6] or "",
            file_size=row[7] or 0, chunk_count=row[8] or 0,
            created_at=_from_ts(row[9]), updated_at=_from_ts(row[10]),
            metadata=_json_loads(row[11]),
            status=row[12], error_message=row[13],
        )

    async def get_source_by_filename(self, filename: str) -> tuple[Source, Notebook] | None:
        cur = await self.db.execute(
            "SELECT s.id, s.notebook_id, s.name, s.type, s.url, s.content, s.file_name, "
            "s.file_size, s.chunk_count, s.created_at, s.updated_at, s.metadata, "
            "COALESCE(s.status, 'pending'), COALESCE(s.error_message, ''), "
            "n.id, n.user_id, n.name, n.description, n.is_public, n.public_token, "
            "n.created_at, n.updated_at, n.metadata "
            "FROM sources s INNER JOIN notebooks n ON s.notebook_id = n.id "
            "WHERE s.file_name = ?",
            (filename,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        source = Source(
            id=row[0], notebook_id=row[1], name=row[2], type=row[3],
            url=row[4] or "", content=row[5] or "", file_name=row[6] or "",
            file_size=row[7] or 0, chunk_count=row[8] or 0,
            created_at=_from_ts(row[9]), updated_at=_from_ts(row[10]),
            metadata=_json_loads(row[11]),
            status=row[12], error_message=row[13],
        )
        notebook = Notebook(
            id=row[14], user_id=row[15] or "", name=row[16],
            description=row[17] or "", is_public=bool(row[18]),
            public_token=row[19] or "",
            created_at=_from_ts(row[20]), updated_at=_from_ts(row[21]),
            metadata=_json_loads(row[22]),
        )
        return source, notebook

    async def list_sources(self, notebook_id: str) -> list[Source]:
        cur = await self.db.execute(
            "SELECT id, notebook_id, name, type, url, content, file_name, file_size, "
            "chunk_count, created_at, updated_at, metadata, "
            "COALESCE(status, 'pending'), COALESCE(error_message, ''), "
            "COALESCE(content_hash, '') "
            "FROM sources WHERE notebook_id = ? ORDER BY created_at DESC",
            (notebook_id,),
        )
        rows = await cur.fetchall()
        return [
            Source(
                id=r[0], notebook_id=r[1], name=r[2], type=r[3],
                url=r[4] or "", content=r[5] or "", file_name=r[6] or "",
                file_size=r[7] or 0, chunk_count=r[8] or 0,
                created_at=_from_ts(r[9]), updated_at=_from_ts(r[10]),
                metadata=_json_loads(r[11]),
                status=r[12], error_message=r[13],
                content_hash=r[14],
            )
            for r in rows
        ]

    async def list_source_fingerprints(
        self, notebook_id: str,
    ) -> list[dict[str, str]]:
        """Return lightweight fingerprint info for all sources in a notebook.

        Only fetches id, name, and content_hash — **never** the multi-MB
        content column.  Used by ``load_notebook_vector_index`` to decide
        which sources need re-ingestion without loading full text.
        """
        cur = await self.db.execute(
            "SELECT id, name, COALESCE(content_hash, '') "
            "FROM sources WHERE notebook_id = ? ORDER BY created_at DESC",
            (notebook_id,),
        )
        rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "content_hash": r[2]}
            for r in rows
        ]

    async def backfill_content_hash(
        self, source_id: str, notebook_id: str, name: str, content: str,
    ) -> str:
        """Compute and persist content_hash for a legacy source."""
        h = self._compute_content_hash(notebook_id, source_id, name, content)
        await self.db.execute(
            "UPDATE sources SET content_hash = ? WHERE id = ?", (h, source_id),
        )
        await self.db.commit()
        return h

    async def delete_source(self, source_id: str) -> None:
        await self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await self.db.commit()

    async def update_source_chunk_count(self, source_id: str, count: int) -> None:
        await self.db.execute(
            "UPDATE sources SET chunk_count = ? WHERE id = ?", (count, source_id)
        )
        await self.db.commit()

    async def update_source_content(
        self, source_id: str, content: str,
        notebook_id: str = "", name: str = "",
    ) -> None:
        """Persist extracted text for a source (called from background ingest).

        Also updates content_hash if notebook_id and name are provided.
        """
        if notebook_id and name and content:
            h = self._compute_content_hash(notebook_id, source_id, name, content)
            await self.db.execute(
                "UPDATE sources SET content = ?, content_hash = ? WHERE id = ?",
                (content, h, source_id),
            )
        else:
            await self.db.execute(
                "UPDATE sources SET content = ? WHERE id = ?", (content, source_id),
            )
        await self.db.commit()

    async def update_source_status(
        self, source_id: str, status: str, error_message: str = "",
    ) -> None:
        """Update the processing status of a source."""
        now = _ts(datetime.utcnow())
        await self.db.execute(
            "UPDATE sources SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
            (status, error_message, now, source_id),
        )
        await self.db.commit()

    # ======================================================================
    # Note operations
    # ======================================================================

    async def create_note(self, note: Note) -> Note:
        note.id = str(uuid4())
        now = datetime.utcnow()
        note.created_at = now
        note.updated_at = now
        await self.db.execute(
            "INSERT INTO notes (id, notebook_id, title, content, type, source_ids, "
            "created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note.id, note.notebook_id, note.title, note.content, note.type,
             json.dumps(note.source_ids), _ts(now), _ts(now), _json_dumps(note.metadata)),
        )
        await self.db.commit()
        return note

    async def get_note(self, note_id: str) -> Note | None:
        cur = await self.db.execute(
            "SELECT id, notebook_id, title, content, type, source_ids, "
            "created_at, updated_at, metadata FROM notes WHERE id = ?",
            (note_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        source_ids = _json_loads(row[5]) if row[5] else []
        if isinstance(source_ids, dict):
            source_ids = []
        return Note(
            id=row[0], notebook_id=row[1], title=row[2], content=row[3],
            type=row[4], source_ids=source_ids if isinstance(source_ids, list) else [],
            created_at=_from_ts(row[6]), updated_at=_from_ts(row[7]),
            metadata=_json_loads(row[8]),
        )

    async def list_notes(self, notebook_id: str) -> list[Note]:
        cur = await self.db.execute(
            "SELECT id, notebook_id, title, content, type, source_ids, "
            "created_at, updated_at, metadata FROM notes WHERE notebook_id = ? "
            "ORDER BY created_at DESC",
            (notebook_id,),
        )
        rows = await cur.fetchall()
        results: list[Note] = []
        for r in rows:
            source_ids = _json_loads(r[5]) if r[5] else []
            if isinstance(source_ids, dict):
                source_ids = []
            results.append(Note(
                id=r[0], notebook_id=r[1], title=r[2], content=r[3],
                type=r[4], source_ids=source_ids if isinstance(source_ids, list) else [],
                created_at=_from_ts(r[6]), updated_at=_from_ts(r[7]),
                metadata=_json_loads(r[8]),
            ))
        return results

    async def get_note_by_filename(self, filename: str) -> tuple[Note, Notebook] | None:
        """Find a note that references ``filename`` in its metadata."""
        cur = await self.db.execute(
            "SELECT n.id, n.notebook_id, n.title, n.content, n.type, n.source_ids, "
            "n.created_at, n.updated_at, n.metadata, "
            "nb.id, nb.user_id, nb.name, nb.description, nb.is_public, nb.public_token, "
            "nb.created_at, nb.updated_at, nb.metadata "
            "FROM notes n INNER JOIN notebooks nb ON n.notebook_id = nb.id",
        )
        rows = await cur.fetchall()
        for r in rows:
            meta = _json_loads(r[8])
            image_url = meta.get("image_url", "")
            if image_url and os.path.basename(image_url) == filename:
                pass  # match found below
            else:
                slides = meta.get("slides", [])
                if isinstance(slides, str):
                    try:
                        slides = json.loads(slides)
                    except Exception:
                        slides = []
                found = any(os.path.basename(s) == filename for s in slides if isinstance(s, str))
                if not found and not (image_url and os.path.basename(image_url) == filename):
                    continue
            source_ids = _json_loads(r[5]) if r[5] else []
            if isinstance(source_ids, dict):
                source_ids = []
            note = Note(
                id=r[0], notebook_id=r[1], title=r[2], content=r[3], type=r[4],
                source_ids=source_ids if isinstance(source_ids, list) else [],
                created_at=_from_ts(r[6]), updated_at=_from_ts(r[7]), metadata=meta,
            )
            nb = Notebook(
                id=r[9], user_id=r[10] or "", name=r[11], description=r[12] or "",
                is_public=bool(r[13]), public_token=r[14] or "",
                created_at=_from_ts(r[15]), updated_at=_from_ts(r[16]),
                metadata=_json_loads(r[17]),
            )
            return note, nb
        return None

    async def delete_note(self, note_id: str) -> None:
        await self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await self.db.commit()

    # ======================================================================
    # Chat operations
    # ======================================================================

    async def create_chat_session(self, notebook_id: str, title: str = "") -> ChatSession:
        sid = str(uuid4())
        now = datetime.utcnow()
        title = title or "New Chat"
        await self.db.execute(
            "INSERT INTO chat_sessions (id, notebook_id, title, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, notebook_id, title, _ts(now), _ts(now), "{}"),
        )
        await self.db.commit()
        return (await self.get_chat_session(sid))  # type: ignore[return-value]

    async def get_chat_session(self, session_id: str) -> ChatSession | None:
        cur = await self.db.execute(
            "SELECT id, notebook_id, title, created_at, updated_at, metadata "
            "FROM chat_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        messages = await self._list_chat_messages(session_id)
        return ChatSession(
            id=row[0], notebook_id=row[1], title=row[2],
            created_at=_from_ts(row[3]), updated_at=_from_ts(row[4]),
            metadata=_json_loads(row[5]), messages=messages,
        )

    async def list_chat_sessions(self, notebook_id: str) -> list[ChatSession]:
        cur = await self.db.execute(
            "SELECT id, notebook_id, title, created_at, updated_at, metadata "
            "FROM chat_sessions WHERE notebook_id = ? ORDER BY updated_at DESC",
            (notebook_id,),
        )
        rows = await cur.fetchall()
        return [
            ChatSession(
                id=r[0], notebook_id=r[1], title=r[2],
                created_at=_from_ts(r[3]), updated_at=_from_ts(r[4]),
                metadata=_json_loads(r[5]),
            )
            for r in rows
        ]

    async def add_chat_message(
        self, session_id: str, role: str, content: str, sources: list[str] | None = None,
    ) -> ChatMessage:
        msg_id = str(uuid4())
        now = datetime.utcnow()
        await self.db.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, sources, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, role, content, json.dumps(sources or []),
             _ts(now), "{}"),
        )
        await self.db.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (_ts(now), session_id),
        )
        await self.db.commit()
        return ChatMessage(
            id=msg_id, session_id=session_id, role=role, content=content,
            sources=sources or [], created_at=now,
        )

    async def _list_chat_messages(self, session_id: str) -> list[ChatMessage]:
        cur = await self.db.execute(
            "SELECT id, session_id, role, content, sources, created_at, metadata "
            "FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        results: list[ChatMessage] = []
        for r in rows:
            sources_raw = r[4]
            sources = []
            if sources_raw:
                try:
                    sources = json.loads(sources_raw)
                except Exception:
                    pass
            results.append(ChatMessage(
                id=r[0], session_id=r[1], role=r[2], content=r[3],
                sources=sources if isinstance(sources, list) else [],
                created_at=_from_ts(r[5]), metadata=_json_loads(r[6]),
            ))
        return results

    async def delete_chat_session(self, session_id: str) -> None:
        await self.db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        await self.db.commit()

    # ======================================================================
    # Activity logging
    # ======================================================================

    async def log_activity(self, log: ActivityLog) -> None:
        if not log.id:
            log.id = str(uuid4())
        if not log.created_at or log.created_at == datetime.min:
            log.created_at = datetime.utcnow()
        await self._ensure_user_exists(log.user_id)
        await self.db.execute(
            "INSERT INTO activity_logs (id, user_id, action, resource_type, resource_id, "
            "resource_name, details, ip_address, user_agent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (log.id, log.user_id, log.action, log.resource_type, log.resource_id,
             log.resource_name, log.details, log.ip_address, log.user_agent,
             _ts(log.created_at)),
        )
        await self.db.commit()

    # ======================================================================
    # Page Index operations
    # ======================================================================

    async def upsert_page_index(
        self,
        notebook_id: str,
        source_id: str,
        page_number: int,
        chunk_count: int,
        first_chunk_idx: int,
        summary_snippet: str = "",
        section_path: str = "",
    ) -> None:
        """Insert or update a page index entry."""
        # Check if exists
        cur = await self.db.execute(
            "SELECT id FROM page_index WHERE source_id = ? AND page_number = ?",
            (source_id, page_number),
        )
        row = await cur.fetchone()
        if row:
            await self.db.execute(
                "UPDATE page_index SET chunk_count = ?, first_chunk_idx = ?, "
                "summary_snippet = ?, section_path = ? WHERE id = ?",
                (chunk_count, first_chunk_idx, summary_snippet[:500],
                 section_path[:500], row[0]),
            )
        else:
            await self.db.execute(
                "INSERT INTO page_index (notebook_id, source_id, page_number, "
                "chunk_count, first_chunk_idx, summary_snippet, section_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (notebook_id, source_id, page_number, chunk_count,
                 first_chunk_idx, summary_snippet[:500], section_path[:500]),
            )
        await self.db.commit()

    async def get_page_index(
        self, source_id: str,
    ) -> list[dict[str, Any]]:
        """Return page index entries for a source, ordered by page number."""
        cur = await self.db.execute(
            "SELECT page_number, chunk_count, first_chunk_idx, summary_snippet, "
            "COALESCE(section_path, '') as section_path "
            "FROM page_index WHERE source_id = ? ORDER BY page_number",
            (source_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "page_number": r[0],
                "chunk_count": r[1],
                "first_chunk_idx": r[2],
                "summary_snippet": r[3] or "",
                "section_path": r[4] or "",
            }
            for r in rows
        ]

    async def delete_page_index(self, source_id: str) -> None:
        """Remove all page index entries for a source."""
        await self.db.execute(
            "DELETE FROM page_index WHERE source_id = ?", (source_id,),
        )
        await self.db.commit()

    async def upsert_section_index(
        self,
        source_id: str,
        section_title: str,
        section_path: str,
        start_page: int,
        end_page: int,
        depth: int = 1,
    ) -> None:
        """Insert or update a section index entry."""
        cur = await self.db.execute(
            "SELECT id FROM section_index WHERE source_id = ? AND section_path = ?",
            (source_id, section_path),
        )
        row = await cur.fetchone()
        if row:
            await self.db.execute(
                "UPDATE section_index SET section_title = ?, start_page = ?, "
                "end_page = ?, depth = ? WHERE id = ?",
                (section_title[:300], start_page, end_page, depth, row[0]),
            )
        else:
            await self.db.execute(
                "INSERT INTO section_index (source_id, section_title, section_path, "
                "start_page, end_page, depth) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, section_title[:300], section_path[:500],
                 start_page, end_page, depth),
            )
        await self.db.commit()

    async def get_section_index(
        self, source_id: str,
    ) -> list[dict[str, Any]]:
        """Return section index entries for a source."""
        cur = await self.db.execute(
            "SELECT section_title, section_path, start_page, end_page, depth "
            "FROM section_index WHERE source_id = ? ORDER BY start_page, depth",
            (source_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "section_title": r[0],
                "section_path": r[1],
                "start_page": r[2],
                "end_page": r[3],
                "depth": r[4],
            }
            for r in rows
        ]

    async def delete_section_index(self, source_id: str) -> None:
        """Remove all section index entries for a source."""
        await self.db.execute(
            "DELETE FROM section_index WHERE source_id = ?", (source_id,),
        )
        await self.db.commit()

    # ======================================================================
    # Guest data cleanup
    # ======================================================================

    async def cleanup_expired_guests(self, max_age_days: int = 30) -> int:
        """Delete guest user data older than *max_age_days*.

        Returns the number of notebooks deleted.
        """
        if max_age_days <= 0:
            return 0
        import time as _time
        cutoff = int(_time.time()) - (max_age_days * 86400)
        cur = await self.db.execute(
            "SELECT id FROM notebooks WHERE user_id LIKE 'guest:%' "
            "AND updated_at < ?",
            (cutoff,),
        )
        rows = await cur.fetchall()
        count = 0
        for row in rows:
            nb_id = row[0]
            await self.db.execute("DELETE FROM notebooks WHERE id = ?", (nb_id,))
            count += 1
        if count:
            await self.db.commit()
            logger.info("Cleaned up %d expired guest notebooks (older than %d days)",
                        count, max_age_days)
        return count
