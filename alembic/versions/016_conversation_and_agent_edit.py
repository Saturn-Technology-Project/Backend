"""Per-user conversation sync and per-agent edit permission

Revision ID: 016_conversation_edit
Revises: 015_org_billing
Create Date: 2026-10-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "016_conversation_edit"
down_revision: Union[str, Sequence[str], None] = "015_org_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    employee_cols = {column["name"] for column in insp.get_columns("employees")}
    if "agent_edit_ids" not in employee_cols:
        op.add_column("employees", sa.Column("agent_edit_ids", sa.JSON(), nullable=True))
    if "conversation_messages" not in insp.get_table_names():
        op.create_table(
            "conversation_messages",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("agent_id", sa.String(length=64), sa.ForeignKey("agents.id"), nullable=False),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("input", sa.Text(), nullable=False),
            sa.Column("output", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("actions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_conversation_messages_organization_id", "conversation_messages", ["organization_id"])
        op.create_index("ix_conversation_messages_agent_id", "conversation_messages", ["agent_id"])
        op.create_index("ix_conversation_messages_user_id", "conversation_messages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_user_id", table_name="conversation_messages")
    op.drop_index("ix_conversation_messages_agent_id", table_name="conversation_messages")
    op.drop_index("ix_conversation_messages_organization_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_column("employees", "agent_edit_ids")
