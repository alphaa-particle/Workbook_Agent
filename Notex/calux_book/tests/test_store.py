"""Tests for calux_book.store — async SQLite CRUD operations."""

from __future__ import annotations

import pytest

from calux_book.models import ActivityLog, Note, Source, User


class TestStoreNotebooks:
    """Notebook CRUD operations."""

    async def test_create_and_get_notebook(self, store):
        nb = await store.create_notebook("user1", "Test NB", "A test notebook")
        assert nb.id
        assert nb.name == "Test NB"
        assert nb.user_id == "user1"
        assert nb.description == "A test notebook"

        fetched = await store.get_notebook(nb.id)
        assert fetched is not None
        assert fetched.name == "Test NB"

    async def test_list_notebooks(self, store):
        await store.create_notebook("user1", "NB1")
        await store.create_notebook("user1", "NB2")
        await store.create_notebook("user2", "NB3")  # different user

        nbs = await store.list_notebooks("user1")
        assert len(nbs) == 2
        names = {nb.name for nb in nbs}
        assert names == {"NB1", "NB2"}

    async def test_list_notebooks_with_stats(self, store):
        nb = await store.create_notebook("user1", "NB Stats")
        # add sources and notes
        src = Source(notebook_id=nb.id, name="src1", type="text", content="hello")
        await store.create_source(src)
        note = Note(notebook_id=nb.id, title="note1", content="world", type="summary")
        await store.create_note(note)

        stats = await store.list_notebooks_with_stats("user1")
        assert len(stats) == 1
        assert stats[0].source_count == 1
        assert stats[0].note_count == 1

    async def test_update_notebook(self, store):
        nb = await store.create_notebook("user1", "Old Name")
        updated = await store.update_notebook(nb.id, "New Name", "New Desc", {"key": "val"})
        assert updated is not None
        assert updated.name == "New Name"
        assert updated.description == "New Desc"

    async def test_delete_notebook(self, store):
        nb = await store.create_notebook("user1", "To Delete")
        await store.delete_notebook(nb.id)
        assert await store.get_notebook(nb.id) is None

    async def test_set_notebook_public(self, store):
        nb = await store.create_notebook("user1", "Public NB")
        assert nb.is_public is False

        public_nb = await store.set_notebook_public(nb.id, True)
        assert public_nb is not None
        assert public_nb.is_public is True
        assert public_nb.public_token

        # Find by public token
        found = await store.get_notebook_by_public_token(public_nb.public_token)
        assert found is not None
        assert found.id == nb.id

        # Make private again
        private_nb = await store.set_notebook_public(nb.id, False)
        assert private_nb.is_public is False

    async def test_list_public_notebooks(self, store):
        nb1 = await store.create_notebook("user1", "Public1")
        nb2 = await store.create_notebook("user1", "Private1")
        await store.set_notebook_public(nb1.id, True)

        public = await store.list_public_notebooks()
        assert len(public) == 1
        assert public[0].name == "Public1"

    async def test_get_nonexistent_notebook(self, store):
        result = await store.get_notebook("nonexistent-id")
        assert result is None


class TestStoreSources:
    """Source CRUD operations."""

    async def test_create_and_get_source(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = Source(notebook_id=nb.id, name="test.txt", type="file", content="Hello world")
        created = await store.create_source(src)
        assert created.id
        assert created.name == "test.txt"

        fetched = await store.get_source(created.id)
        assert fetched is not None
        assert fetched.content == "Hello world"

    async def test_list_sources(self, store):
        nb = await store.create_notebook("user1", "NB")
        for i in range(3):
            await store.create_source(Source(
                notebook_id=nb.id, name=f"src{i}.txt", type="text", content=f"Content {i}",
            ))
        sources = await store.list_sources(nb.id)
        assert len(sources) == 3

    async def test_delete_source(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="delete.txt", type="text", content="bye",
        ))
        await store.delete_source(src.id)
        assert await store.get_source(src.id) is None

    async def test_update_chunk_count(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="chunks.txt", type="text", content="data",
        ))
        await store.update_source_chunk_count(src.id, 42)
        updated = await store.get_source(src.id)
        assert updated.chunk_count == 42

    async def test_get_source_by_filename(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="original.txt", type="file",
            file_name="unique_name_abc.txt", content="data",
        ))
        result = await store.get_source_by_filename("unique_name_abc.txt")
        assert result is not None
        source, notebook = result
        assert source.id == src.id
        assert notebook.id == nb.id


class TestStoreNotes:
    """Note CRUD operations."""

    async def test_create_and_get_note(self, store):
        nb = await store.create_notebook("user1", "NB")
        note = await store.create_note(Note(
            notebook_id=nb.id, title="Summary", content="A summary.", type="summary",
            source_ids=["s1", "s2"],
        ))
        assert note.id
        assert note.title == "Summary"

        fetched = await store.get_note(note.id)
        assert fetched is not None
        assert fetched.source_ids == ["s1", "s2"]

    async def test_list_notes(self, store):
        nb = await store.create_notebook("user1", "NB")
        await store.create_note(Note(
            notebook_id=nb.id, title="N1", content="c1", type="summary",
        ))
        await store.create_note(Note(
            notebook_id=nb.id, title="N2", content="c2", type="custom",
        ))
        notes = await store.list_notes(nb.id)
        assert len(notes) == 2

    async def test_delete_note(self, store):
        nb = await store.create_notebook("user1", "NB")
        note = await store.create_note(Note(
            notebook_id=nb.id, title="Del", content="c", type="summary",
        ))
        await store.delete_note(note.id)
        assert await store.get_note(note.id) is None


class TestStoreChatSessions:
    """Chat session and message operations."""

    async def test_create_session(self, store):
        nb = await store.create_notebook("user1", "NB")
        session = await store.create_chat_session(nb.id, "Test Chat")
        assert session.id
        assert session.title == "Test Chat"
        assert session.notebook_id == nb.id

    async def test_default_title(self, store):
        nb = await store.create_notebook("user1", "NB")
        session = await store.create_chat_session(nb.id)
        assert session.title == "New Chat"

    async def test_add_messages(self, store):
        nb = await store.create_notebook("user1", "NB")
        session = await store.create_chat_session(nb.id)

        msg1 = await store.add_chat_message(session.id, "user", "Hello?")
        assert msg1.role == "user"
        assert msg1.content == "Hello?"

        msg2 = await store.add_chat_message(session.id, "assistant", "Hi!", ["src1"])
        assert msg2.sources == ["src1"]

        # Verify messages are in session
        fetched = await store.get_chat_session(session.id)
        assert fetched is not None
        assert len(fetched.messages) == 2

    async def test_list_sessions(self, store):
        nb = await store.create_notebook("user1", "NB")
        await store.create_chat_session(nb.id, "S1")
        await store.create_chat_session(nb.id, "S2")
        sessions = await store.list_chat_sessions(nb.id)
        assert len(sessions) == 2

    async def test_delete_session(self, store):
        nb = await store.create_notebook("user1", "NB")
        session = await store.create_chat_session(nb.id, "Del Session")
        await store.delete_chat_session(session.id)
        assert await store.get_chat_session(session.id) is None


class TestStoreUsers:
    """User operations."""

    async def test_create_user(self, store):
        user = User(email="test@example.com", name="Test", provider="github")
        await store.create_user(user)
        assert user.id  # should be set

        fetched = await store.get_user(user.id)
        assert fetched is not None
        assert fetched.email == "test@example.com"

    async def test_get_user_by_email(self, store):
        user = User(email="search@test.com", name="Search", provider="google")
        await store.create_user(user)
        found = await store.get_user_by_email("search@test.com")
        assert found is not None
        assert found.name == "Search"

    async def test_update_existing_user(self, store):
        user = User(email="dup@test.com", name="V1", provider="github")
        await store.create_user(user)

        user2 = User(email="dup@test.com", name="V2", provider="google")
        await store.create_user(user2)

        fetched = await store.get_user_by_email("dup@test.com")
        assert fetched.name == "V2"

    async def test_get_nonexistent_user(self, store):
        result = await store.get_user("no-such-user")
        assert result is None


class TestStoreActivityLog:
    """Activity logging operations."""

    async def test_log_activity(self, store):
        # Just ensure it doesn't raise
        await store._ensure_user_exists("user1")
        log = ActivityLog(
            user_id="user1", action="test", resource_type="notebook",
            resource_id="nb1", resource_name="Test NB",
        )
        await store.log_activity(log)
        assert log.id  # should be assigned


class TestStorePageIndex:
    """Page index CRUD operations."""

    async def test_upsert_and_get_page_index(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="doc.pdf", type="file", content="hello",
        ))
        await store.upsert_page_index(
            nb.id, src.id, 1, chunk_count=3, first_chunk_idx=0,
            section_path="Ch1 > Intro",
        )
        await store.upsert_page_index(
            nb.id, src.id, 2, chunk_count=2, first_chunk_idx=3,
            section_path="Ch1 > Background",
        )
        pages = await store.get_page_index(src.id)
        assert len(pages) == 2
        assert pages[0]["page_number"] == 1
        assert pages[0]["section_path"] == "Ch1 > Intro"
        assert pages[1]["page_number"] == 2

    async def test_upsert_page_index_updates(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="doc.pdf", type="file", content="hello",
        ))
        await store.upsert_page_index(
            nb.id, src.id, 1, chunk_count=2, first_chunk_idx=0,
            section_path="Old",
        )
        await store.upsert_page_index(
            nb.id, src.id, 1, chunk_count=5, first_chunk_idx=0,
            section_path="New",
        )
        pages = await store.get_page_index(src.id)
        assert len(pages) == 1
        assert pages[0]["chunk_count"] == 5
        assert pages[0]["section_path"] == "New"

    async def test_get_page_index_empty(self, store):
        pages = await store.get_page_index("nonexistent")
        assert pages == []


class TestStoreSectionIndex:
    """Section index CRUD operations."""

    async def test_upsert_and_get_section_index(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="book.pdf", type="file", content="data",
        ))
        await store.upsert_section_index(
            src.id, "Introduction", "Ch1 > Introduction", 1, 3, 2,
        )
        await store.upsert_section_index(
            src.id, "Background", "Ch1 > Background", 4, 8, 2,
        )
        sections = await store.get_section_index(src.id)
        assert len(sections) == 2
        assert sections[0]["section_title"] == "Introduction"
        assert sections[0]["start_page"] == 1
        assert sections[1]["section_title"] == "Background"

    async def test_delete_section_index(self, store):
        nb = await store.create_notebook("user1", "NB")
        src = await store.create_source(Source(
            notebook_id=nb.id, name="book.pdf", type="file", content="data",
        ))
        await store.upsert_section_index(
            src.id, "Intro", "Intro", 1, 5, 1,
        )
        await store.delete_section_index(src.id)
        sections = await store.get_section_index(src.id)
        assert sections == []
