"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "analysis_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_analysis_requests_user_id", "analysis_requests", ["user_id"])
    op.create_index("ix_analysis_requests_created_at", "analysis_requests", ["created_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id_hash", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_user_id_hash", "audit_events", ["user_id_hash"])
    op.create_index("ix_audit_events_operation", "audit_events", ["operation"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])

    op.create_table(
        "telegram_chats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("chat_type", sa.String(length=32), nullable=False),
        sa.Column("access_state", sa.String(length=32), nullable=False),
        sa.Column("access_error", sa.String(length=255), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("sync_state", sa.String(length=16), nullable=False),
        sa.Column("sync_cursor", sa.BigInteger(), nullable=True),
        sa.Column("sync_estimate", sa.BigInteger(), nullable=True),
        sa.Column("sync_indexed_count", sa.Integer(), nullable=False),
        sa.Column("sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "telegram_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_message_chat_msg"),
    )
    op.create_index("ix_telegram_messages_chat_id", "telegram_messages", ["chat_id"])
    op.create_index("ix_telegram_messages_date", "telegram_messages", ["date"])

    op.create_table(
        "message_terms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("term", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "term", "message_id", name="uq_term_msg"),
    )
    op.create_index("ix_message_terms_chat_id", "message_terms", ["chat_id"])
    op.create_index("ix_message_terms_term", "message_terms", ["term"])

    op.create_table(
        "phrase_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("term", sa.String(length=128), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_message_id", sa.BigInteger(), nullable=True),
        sa.Column("sample_context", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chat_id", "term", name="uq_phrase_chat_term"),
    )
    op.create_index("ix_phrase_occurrences_chat_id", "phrase_occurrences", ["chat_id"])
    op.create_index("ix_phrase_occurrences_term", "phrase_occurrences", ["term"])

    op.create_table(
        "rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_allowlist", sa.Boolean(), nullable=False),
        sa.Column("case_sensitive", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["telegram_chats.telegram_chat_id"]),
    )

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "analysis_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("overall_score", sa.Integer(), nullable=False),
        sa.Column("overall_level", sa.String(length=16), nullable=False),
        sa.Column("global_result", sa.JSON(), nullable=False),
        sa.Column("chat_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["analysis_requests.id"]),
    )


def downgrade() -> None:
    op.drop_table("analysis_results")
    op.drop_table("user_settings")
    op.drop_table("rules")
    op.drop_table("phrase_occurrences")
    op.drop_table("message_terms")
    op.drop_table("telegram_messages")
    op.drop_table("telegram_chats")
    op.drop_table("audit_events")
    op.drop_table("analysis_requests")
    op.drop_table("app_state")