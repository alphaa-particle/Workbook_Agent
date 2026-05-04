"""Tests for calux_book.models — Pydantic model construction and serialization."""

from __future__ import annotations

from datetime import datetime

import pytest

from calux_book.models import (
    ActivityLog,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatSession,
    ErrorResponse,
    HealthResponse,
    Note,
    Notebook,
    NotebookWithStats,
    Source,
    SourceSummary,
    TransformationRequest,
    TransformationResponse,
    User,
)


class TestUserModel:
    def test_defaults(self):
        u = User()
        assert u.id == ""
        assert u.provider == ""
        assert isinstance(u.created_at, datetime)

    def test_construction(self):
        u = User(id="u1", email="a@b.com", name="Alice", provider="github")
        assert u.email == "a@b.com"

    def test_serialization(self):
        u = User(id="u1", email="a@b.com", name="Alice")
        d = u.model_dump()
        assert d["id"] == "u1"
        assert "email" in d


class TestNotebookModel:
    def test_defaults(self):
        nb = Notebook()
        assert nb.is_public is False
        assert nb.metadata == {}

    def test_with_stats(self):
        nbs = NotebookWithStats(
            id="nb1", name="Test", source_count=5, note_count=3,
        )
        assert nbs.source_count == 5
        assert nbs.note_count == 3


class TestSourceModel:
    def test_construction(self):
        s = Source(
            notebook_id="nb1", name="file.txt", type="file",
            content="Hello", file_size=5,
        )
        assert s.file_size == 5
        assert s.chunk_count == 0


class TestNoteModel:
    def test_source_ids(self):
        n = Note(source_ids=["s1", "s2"])
        assert len(n.source_ids) == 2


class TestChatModels:
    def test_chat_message(self):
        msg = ChatMessage(role="user", content="Hello")
        assert msg.role == "user"

    def test_chat_session(self):
        s = ChatSession(notebook_id="nb1", title="Chat")
        assert s.messages == []

    def test_chat_request(self):
        r = ChatRequest(message="How?")
        assert r.session_id == ""

    def test_chat_response(self):
        r = ChatResponse(message="Answer", sources=[SourceSummary(id="s1", name="src")])
        assert len(r.sources) == 1


class TestTransformationModels:
    def test_request_defaults(self):
        r = TransformationRequest()
        assert r.type == "summary"
        assert r.length == "medium"

    def test_response(self):
        r = TransformationResponse(type="summary", content="A summary")
        assert r.content == "A summary"


class TestErrorAndHealth:
    def test_error_response(self):
        e = ErrorResponse(error="Not found")
        assert e.error == "Not found"

    def test_health_response(self):
        h = HealthResponse(status="ok", version="1.0.0", timestamp=123456)
        assert h.status == "ok"


class TestActivityLog:
    def test_defaults(self):
        a = ActivityLog(user_id="u1", action="test")
        assert a.resource_type == ""
        assert isinstance(a.created_at, datetime)
