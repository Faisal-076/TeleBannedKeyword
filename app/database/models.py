"""ORM models.

Storage policy (privacy-first):
- No credentials, tokens, keys or session material is ever stored here.
- Telegram messages are stored only as extracted analysis data: text is kept
  for indexed messages up to INDEXED_TEXT_MAX, terms are extracted tokens,
  and context snippets are trimmed to a fixed window.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.timeutil import utc_now_naive

INDEXED_TEXT_MAX = 1000
CONTEXT_SNIPPET_MAX = 300
TERM_MAX = 128
TITLE_MAX = 256


class Base(DeclarativeBase):
    pass


class ChatType(str, enum.Enum):
    CHANNEL = "channel"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    DISCUSSION = "discussion"
    FORUM = "forum"
    USER = "user"
    UNKNOWN = "unknown"


class AccessState(str, enum.Enum):
    ACCESSIBLE = "accessible"
    NOT_MEMBER = "not_member"
    BANNED = "banned"
    RESTRICTED = "restricted"
    PRIVATE_NO_ACCESS = "private_no_access"
    DELETED = "deleted"
    MIGRATED = "migrated"
    USERNAME_NOT_FOUND = "username_not_found"
    INSUFFICIENT_PERMISSIONS = "insufficient_permissions"
    NOT_VERIFIED = "not_verified"
    ERROR = "error"


class SyncState(str, enum.Enum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE = "done"
    FAILED = "failed"


class AnalysisStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class RuleScope(str, enum.Enum):
    GLOBAL = "global"
    CHAT = "chat"


class RuleKind(str, enum.Enum):
    EXACT = "exact"
    PHRASE = "phrase"
    REGEX = "regex"


class TargetChat(Base):
    """A chat configured for analysis by the operator."""

    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(TITLE_MAX), default="")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_type: Mapped[str] = mapped_column(String(32), default=ChatType.UNKNOWN.value)
    access_state: Mapped[str] = mapped_column(
        String(32), default=AccessState.NOT_VERIFIED.value
    )
    access_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional topic/thread identifier (forum topic id).
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sync state machine.
    sync_state: Mapped[str] = mapped_column(String(16), default=SyncState.NONE.value)
    sync_cursor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sync_estimate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sync_indexed_count: Mapped[int] = mapped_column(Integer, default=0)
    sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive
    )

    rules: Mapped[list["Rule"]] = relationship(
        "Rule", back_populates="chat", cascade="all, delete-orphan"
    )


class IndexedMessage(Base):
    """A message stored from a target chat during history sync."""

    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_message_chat_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)  # trimmed to INDEXED_TEXT_MAX
    normalized_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )


class MessageTerm(Base):
    """Extracted term occurrence inside an indexed message."""

    __tablename__ = "message_terms"
    __table_args__ = (
        UniqueConstraint("chat_id", "term", "message_id", name="uq_term_msg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    term: Mapped[str] = mapped_column(String(TERM_MAX), index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )


class PhraseOccurrence(Base):
    """Aggregated per-chat term statistics with a small context sample."""

    __tablename__ = "phrase_occurrences"
    __table_args__ = (
        UniqueConstraint("chat_id", "term", name="uq_phrase_chat_term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    term: Mapped[str] = mapped_column(String(TERM_MAX), index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sample_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sample_context: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # trimmed snippet
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )


class Rule(Base):
    """Moderation rule. Database-backed, editable without code changes."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), default=RuleScope.GLOBAL.value)
    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("telegram_chats.telegram_chat_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), default=RuleKind.EXACT.value)
    pattern: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(128), default="general")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_allowlist: Mapped[bool] = mapped_column(Boolean, default=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )

    chat: Mapped[TargetChat | None] = relationship("TargetChat", back_populates="rules")


class AnalysisRequest(Base):
    """A user-submitted draft message awaiting/undergoing analysis."""

    __tablename__ = "analysis_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    original_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=AnalysisStatus.QUEUED.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    result: Mapped["AnalysisResult | None"] = relationship(
        "AnalysisResult", back_populates="request", uselist=False,
        cascade="all, delete-orphan",
    )


class AnalysisResult(Base):
    """Persisted analysis output for one request."""

    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("analysis_requests.id"), unique=True
    )
    overall_score: Mapped[int] = mapped_column(Integer, default=0)
    overall_level: Mapped[str] = mapped_column(String(16), default="LOW")
    global_result: Mapped[dict] = mapped_column(JSON)  # summary + recommendations
    chat_results: Mapped[list] = mapped_column(JSON)  # per-chat findings
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )

    request: Mapped[AnalysisRequest] = relationship(
        "AnalysisRequest", back_populates="result"
    )


class UserSetting(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="admin")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )


class AuditEvent(Base):
    """Security-relevant operations. Never stores credentials or message content."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, index=True
    )


class AppState(Base):
    """Key/value operational state (session status, connection stamps)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive
    )
