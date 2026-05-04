"""Pydantic data models for Calux Book.

All domain entities are defined here with proper serialization support.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

class User(BaseModel):
    id: str = ""
    email: str = ""
    name: str = ""
    avatar_url: str = ""
    provider: str = ""  # google, github, guest
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Source(BaseModel):
    id: str = ""
    notebook_id: str = ""
    name: str = ""
    type: str = ""         # file, url, text, youtube
    url: str = ""
    content: str = ""
    file_name: str = ""
    file_size: int = 0
    chunk_count: int = 0
    status: str = "pending"  # pending, extracting, embedding, ready, error
    error_message: str = ""
    content_hash: str = ""   # SHA-256 fingerprint of content for fast dedup
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Note(BaseModel):
    id: str = ""
    notebook_id: str = ""
    title: str = ""
    content: str = ""
    type: str = ""         # summary, custom
    source_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Notebook(BaseModel):
    id: str = ""
    user_id: str = ""
    name: str = ""
    description: str = ""
    is_public: bool = False
    public_token: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotebookWithStats(Notebook):
    source_count: int = 0
    note_count: int = 0
    cover_image_url: str = ""


class ChatMessage(BaseModel):
    id: str = ""
    session_id: str = ""
    role: str = ""         # user, assistant, system
    content: str = ""
    sources: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatSession(BaseModel):
    id: str = ""
    notebook_id: str = ""
    title: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityLog(BaseModel):
    id: str = ""
    user_id: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    resource_name: str = ""
    details: str = ""
    ip_address: str = ""
    user_agent: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TransformationRequest(BaseModel):
    type: str = "summary"       # summary, custom
    prompt: str = ""
    source_ids: list[str] = Field(default_factory=list)
    length: str = "medium"      # short, medium, long
    format: str = "markdown"    # markdown, bullet_points, paragraphs


class TransformationResponse(BaseModel):
    id: str = ""
    type: str = ""
    content: str = ""
    sources: list[SourceSummary] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceSummary(BaseModel):
    id: str = ""
    name: str = ""
    type: str = ""


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    message: str = ""
    sources: list[SourceSummary] = Field(default_factory=list)
    session_id: str = ""
    message_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: str
    code: str = ""
    details: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    timestamp: int = 0
    services: dict[str, str] = Field(default_factory=dict)
