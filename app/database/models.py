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
    """A Telegram chat configured for analysis by one or more users.

    Global metadata shared across users (title, username, type).
    Ownership and per-user settings live in ``UserChatTarget``.
    """

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


class AuthUserStatus(str, enum.Enum):
    AUTHORIZED = "authorized"
    REVOKED = "revoked"


class AuthUserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class AuthorizedUser(Base):
    """Bot users authorized by root admins.  Canonical identity = telegram_user_id."""

    __tablename__ = "authorized_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=AuthUserStatus.AUTHORIZED.value, index=True)
    role: Mapped[str] = mapped_column(String(16), default=AuthUserRole.USER.value)
    authorized_by: Mapped[int] = mapped_column(BigInteger)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive
    )


class PendingAction(Base):
    """Server-side record for inline-callback confirmations (authorize / revoke).
    Callback data contains a random token; the handler resolves it here to
    verify the actor, target and expiry."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    action: Mapped[str] = mapped_column(String(16))  # "authorize" | "revoke"
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    target_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )


class AccessMode(str, enum.Enum):
    CENTRAL_PUBLIC = "central_public"
    USER_SESSION = "user_session"
    UNAVAILABLE = "unavailable"


class AccessIdentityStatus(str, enum.Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    ABSENT = "absent"
    INVALID = "invalid"


class TelegramAccessIdentity(Base):
    """A user-provisioned Telegram session identity for private-target access."""

    __tablename__ = "telegram_access_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=AccessIdentityStatus.ABSENT.value
    )
    session_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive
    )


class UserChatTarget(Base):
    """Per-user target chat configuration.  Two users may independently
    configure the same Telegram chat — ownership is per-user."""

    __tablename__ = "user_chat_targets"
    __table_args__ = (
        UniqueConstraint("user_id", "telegram_chat_id", name="uq_user_chat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    access_mode: Mapped[str] = mapped_column(
        String(16), default=AccessMode.CENTRAL_PUBLIC.value
    )
    access_identity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now_naive, onupdate=utc_now_naive
    )
