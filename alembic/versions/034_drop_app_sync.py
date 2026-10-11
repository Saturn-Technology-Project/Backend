"""Drop app agent sync tables now owned by Saturn AI Server

Revision ID: 034_drop_app_sync
Revises: 033_password_reset_hash
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "034_drop_app_sync"
down_revision: Union[str, Sequence[str], None] = "033_password_reset_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "conversation_messages",
    "user_app_state",
    "org_app_state",
    "user_app_mcp",
    "org_app_mcp",
)


def upgrade() -> None:
    bind = op.get_bind()
    present = set(inspect(bind).get_table_names())
    for name in _TABLES:
        if name in present:
            op.drop_table(name)


def downgrade() -> None:
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=64), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_app_state",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("wakes", sa.JSON(), nullable=False),
        sa.Column("memories", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "org_app_state",
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "user_app_mcp",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("servers", sa.JSON(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "org_app_mcp",
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("servers", sa.JSON(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
