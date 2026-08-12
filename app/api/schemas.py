"""Admin API request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatAddRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=512)


class ChatAddResponse(BaseModel):
    ok: bool
    chat_id: int | None = None
    title: str | None = None
    username: str | None = None
    chat_type: str | None = None
    error: str | None = None


class ChatListResponse(BaseModel):
    chats: list[dict]


class SyncRequest(BaseModel):
    mode: str = "incremental"


class RuleCreateRequest(BaseModel):
    scope: str = "global"
    kind: str = "exact"
    pattern: str = Field(min_length=1)
    category: str = "general"
    chat_id: int | None = None
    allow: bool = False
    case_sensitive: bool = False
    weight: float | None = None
    note: str | None = None
    enabled: bool = True


class RuleResponse(BaseModel):
    id: int
    scope: str
    kind: str
    pattern: str
    category: str
    chat_id: int | None = None
    allow: bool
    enabled: bool
    weight: float | None = None
    note: str | None = None
