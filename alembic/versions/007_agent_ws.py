"""agent websocket enrollment tokens

Revision ID: 007_agent_ws
Revises: 006_platform_admin
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "007_agent_ws"
down_revision: Union[str, Sequence[str], None] = "006_platform_admin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("agents")}
    if "token_hash" not in cols:
        op.add_column("agents", sa.Column("token_hash", sa.String(length=64), nullable=True))
        op.create_index("ix_agents_token_hash", "agents", ["token_hash"])
    if "last_seen_at" not in cols:
        op.add_column(
            "agents",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_index("ix_agents_token_hash", table_name="agents")
    op.drop_column("agents", "last_seen_at")
    op.drop_column("agents", "token_hash")
