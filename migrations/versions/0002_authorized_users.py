"""add authorized_users + pending_actions

Revision ID: 0002_authorized_users
Revises: 0001_initial
Create Date: 2026-08-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_authorized_users"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authorized_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="authorized"),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("authorized_by", sa.BigInteger(), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id"),
    )
    op.create_index(op.f("ix_authorized_users_telegram_user_id"), "authorized_users", ["telegram_user_id"])
    op.create_index(op.f("ix_authorized_users_status"), "authorized_users", ["status"])

    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_username", sa.String(128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(op.f("ix_pending_actions_token"), "pending_actions", ["token"])
    op.create_index(op.f("ix_pending_actions_actor_id"), "pending_actions", ["actor_id"])
    op.create_index(op.f("ix_pending_actions_expires_at"), "pending_actions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("pending_actions")
    op.drop_table("authorized_users")
