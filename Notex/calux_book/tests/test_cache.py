"""Tests for calux_book.cache — TTLCache and CachedStore."""

from __future__ import annotations

import asyncio
import time

import pytest


class TestTTLCache:
    """TTL cache basic operations."""

    async def test_set_and_get(self):
        from calux_book.cache import TTLCache

        cache = TTLCache(ttl_seconds=60)
        await cache.set("key1", "value1")
        assert await cache.get("key1") == "value1"

    async def test_get_missing_key(self):
        from calux_book.cache import TTLCache

        cache = TTLCache()
        assert await cache.get("missing") is None

    async def test_delete(self):
        from calux_book.cache import TTLCache

        cache = TTLCache()
        await cache.set("key1", "val")
        await cache.delete("key1")
        assert await cache.get("key1") is None

    async def test_ttl_expiration(self):
        from calux_book.cache import TTLCache

        cache = TTLCache(ttl_seconds=0.05)  # 50ms TTL
        await cache.set("expire", "data")
        assert await cache.get("expire") == "data"
        await asyncio.sleep(0.1)
        assert await cache.get("expire") is None

    async def test_invalidate_pattern(self):
        from calux_book.cache import TTLCache

        cache = TTLCache()
        await cache.set("prefix:a", 1)
        await cache.set("prefix:b", 2)
        await cache.set("other:c", 3)
        await cache.invalidate_pattern("prefix:")
        assert await cache.get("prefix:a") is None
        assert await cache.get("prefix:b") is None
        assert await cache.get("other:c") == 3

    async def test_clear(self):
        from calux_book.cache import TTLCache

        cache = TTLCache()
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.clear()
        assert cache.size == 0

    async def test_cleanup(self):
        from calux_book.cache import TTLCache

        cache = TTLCache(ttl_seconds=0.05)
        await cache.set("old", "data")
        await asyncio.sleep(0.1)
        await cache.cleanup()
        assert cache.size == 0


class TestCachedStore:
    """CachedStore wrapping a real Store."""

    async def test_notebooks_cached(self, store):
        from calux_book.cache import CachedStore

        cached = CachedStore(store, ttl_seconds=60)
        nb = await cached.create_notebook("user1", "Cached NB")
        assert nb.name == "Cached NB"

        # Second call should hit cache
        nbs = await cached.list_notebooks("user1")
        assert len(nbs) == 1

        nbs2 = await cached.list_notebooks("user1")
        assert len(nbs2) == 1

    async def test_notebook_cache_invalidation_on_delete(self, store):
        from calux_book.cache import CachedStore

        cached = CachedStore(store, ttl_seconds=60)
        nb = await cached.create_notebook("user1", "Del NB")

        # Populate cache
        await cached.list_notebooks("user1")

        # Delete should invalidate
        await cached.delete_notebook(nb.id)
        nbs = await cached.list_notebooks("user1")
        assert len(nbs) == 0

    async def test_source_operations(self, store):
        from calux_book.cache import CachedStore
        from calux_book.models import Source

        cached = CachedStore(store, ttl_seconds=60)
        nb = await cached.create_notebook("user1", "NB")
        src = await cached.create_source(Source(
            notebook_id=nb.id, name="s1.txt", type="text", content="hello",
        ))
        sources = await cached.list_sources(nb.id)
        assert len(sources) == 1

        await cached.delete_source(src.id)
        sources = await cached.list_sources(nb.id)
        assert len(sources) == 0
