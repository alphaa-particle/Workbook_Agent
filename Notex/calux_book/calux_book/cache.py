"""In-memory TTL cache wrapping the Store for Calux Book."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class TTLCache:
    """Thread-safe in-memory cache with per-entry TTL and max-size LRU eviction."""

    def __init__(self, ttl_seconds: float = 300.0, max_size: int = 512) -> None:
        self._data: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._data[key]
                return None
            # Move to end (LRU touch)
            self._data[key] = entry
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            # Evict oldest entries if we're at capacity
            while len(self._data) >= self._max_size:
                oldest_key = next(iter(self._data))
                del self._data[oldest_key]
            self._data[key] = (value, time.monotonic() + self._ttl)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def invalidate_pattern(self, prefix: str) -> None:
        async with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    async def cleanup(self) -> None:
        async with self._lock:
            now = time.monotonic()
            expired = [k for k, (_, exp) in self._data.items() if now > exp]
            for k in expired:
                del self._data[k]

    @property
    def size(self) -> int:
        return len(self._data)


class CachedStore:
    """Wraps a Store instance with TTL caching for frequent read paths."""

    def __init__(self, store: Any, ttl_seconds: float = 300.0) -> None:
        from .store import Store
        self.store: Store = store
        self.cache = TTLCache(ttl_seconds)

    # -- key helpers ----------------------------------------------------------
    @staticmethod
    def _nb_list(user_id: str) -> str:
        return f"notebooks:list:{user_id}"

    @staticmethod
    def _nb(nb_id: str) -> str:
        return f"notebook:{nb_id}"

    @staticmethod
    def _notes(nb_id: str) -> str:
        return f"notes:{nb_id}"

    @staticmethod
    def _sources(nb_id: str) -> str:
        return f"sources:{nb_id}"

    @staticmethod
    def _sessions(nb_id: str) -> str:
        return f"chat_sessions:{nb_id}"

    # -- delegated + cached methods -------------------------------------------
    async def list_notebooks(self, user_id: str):
        key = self._nb_list(user_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.list_notebooks(user_id)
        await self.cache.set(key, result)
        return result

    async def list_notebooks_with_stats(self, user_id: str):
        key = self._nb_list(user_id) + ":stats"
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.list_notebooks_with_stats(user_id)
        await self.cache.set(key, result)
        return result

    async def get_notebook(self, nb_id: str):
        key = self._nb(nb_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.get_notebook(nb_id)
        if result:
            await self.cache.set(key, result)
        return result

    async def create_notebook(self, user_id: str, name: str, description: str = "", metadata=None):
        result = await self.store.create_notebook(user_id, name, description, metadata)
        await self.cache.delete(self._nb_list(user_id))
        await self.cache.delete(self._nb_list(user_id) + ":stats")
        return result

    async def update_notebook(self, nb_id: str, name: str, description: str, metadata=None):
        result = await self.store.update_notebook(nb_id, name, description, metadata)
        await self.cache.delete(self._nb(nb_id))
        if result and result.user_id:
            await self.cache.delete(self._nb_list(result.user_id))
            await self.cache.delete(self._nb_list(result.user_id) + ":stats")
        return result

    async def delete_notebook(self, nb_id: str):
        nb = await self.store.get_notebook(nb_id)
        await self.store.delete_notebook(nb_id)
        await self.cache.delete(self._nb(nb_id))
        if nb and nb.user_id:
            await self.cache.delete(self._nb_list(nb.user_id))
            await self.cache.delete(self._nb_list(nb.user_id) + ":stats")
        await self.cache.invalidate_pattern(self._notes(nb_id))
        await self.cache.invalidate_pattern(self._sources(nb_id))
        await self.cache.invalidate_pattern(self._sessions(nb_id))

    async def set_notebook_public(self, nb_id: str, is_public: bool):
        result = await self.store.set_notebook_public(nb_id, is_public)
        await self.cache.delete(self._nb(nb_id))
        return result

    async def get_notebook_by_public_token(self, token: str):
        return await self.store.get_notebook_by_public_token(token)

    async def list_public_notebooks(self):
        return await self.store.list_public_notebooks()

    # Sources
    async def list_sources(self, notebook_id: str):
        key = self._sources(notebook_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.list_sources(notebook_id)
        await self.cache.set(key, result)
        return result

    async def create_source(self, source):
        result = await self.store.create_source(source)
        await self.cache.delete(self._sources(source.notebook_id))
        return result

    async def get_source(self, source_id: str):
        return await self.store.get_source(source_id)

    async def get_source_by_filename(self, filename: str):
        return await self.store.get_source_by_filename(filename)

    async def delete_source(self, source_id: str):
        source = await self.store.get_source(source_id)
        await self.store.delete_source(source_id)
        if source:
            await self.cache.delete(self._sources(source.notebook_id))

    async def update_source_chunk_count(self, source_id: str, count: int):
        await self.store.update_source_chunk_count(source_id, count)

    async def update_source_content(
        self, source_id: str, content: str,
        notebook_id: str = "", name: str = "",
    ):
        await self.store.update_source_content(
            source_id, content, notebook_id=notebook_id, name=name,
        )

    async def update_source_status(
        self, source_id: str, status: str, error_message: str = "",
    ):
        await self.store.update_source_status(source_id, status, error_message)

    # Notes
    async def list_notes(self, notebook_id: str):
        key = self._notes(notebook_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.list_notes(notebook_id)
        await self.cache.set(key, result)
        return result

    async def create_note(self, note):
        result = await self.store.create_note(note)
        await self.cache.delete(self._notes(note.notebook_id))
        return result

    async def get_note(self, note_id: str):
        return await self.store.get_note(note_id)

    async def get_note_by_filename(self, filename: str):
        return await self.store.get_note_by_filename(filename)

    async def delete_note(self, note_id: str):
        note = await self.store.get_note(note_id)
        await self.store.delete_note(note_id)
        if note:
            await self.cache.delete(self._notes(note.notebook_id))

    # Chat
    async def list_chat_sessions(self, notebook_id: str):
        key = self._sessions(notebook_id)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached
        result = await self.store.list_chat_sessions(notebook_id)
        await self.cache.set(key, result)
        return result

    async def create_chat_session(self, notebook_id: str, title: str = ""):
        result = await self.store.create_chat_session(notebook_id, title)
        await self.cache.delete(self._sessions(notebook_id))
        return result

    async def get_chat_session(self, session_id: str):
        return await self.store.get_chat_session(session_id)

    async def add_chat_message(self, session_id: str, role: str, content: str, sources=None):
        return await self.store.add_chat_message(session_id, role, content, sources)

    async def delete_chat_session(self, session_id: str):
        session = await self.store.get_chat_session(session_id)
        await self.store.delete_chat_session(session_id)
        if session:
            await self.cache.delete(self._sessions(session.notebook_id))

    # Activity
    async def log_activity(self, log):
        await self.store.log_activity(log)

    # Pass-through
    async def create_user(self, user):
        await self.store.create_user(user)

    async def get_user(self, user_id: str):
        return await self.store.get_user(user_id)

    async def get_user_by_email(self, email: str):
        return await self.store.get_user_by_email(email)

    # Page index (pass-through, no caching needed)
    async def get_page_index(self, source_id: str):
        return await self.store.get_page_index(source_id)

    async def upsert_page_index(self, *args, **kwargs):
        return await self.store.upsert_page_index(*args, **kwargs)
