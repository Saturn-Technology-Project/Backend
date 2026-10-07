"""Sync MCP that users connect inside Saturn App

Revision ID: 020_user_app_mcp
Revises: 019_org_kind
Create Date: 2026-10-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "020_user_app_mcp"
down_revision: Union[str, Sequence[str], None] = "019_org_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "user_app_mcp" in set(insp.get_table_names()):
        return
    op.create_table(
        "user_app_mcp",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("servers", sa.JSON(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "user_app_mcp" in set(insp.get_table_names()):
        op.drop_table("user_app_mcp")
