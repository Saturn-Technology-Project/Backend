"""Store team MCP servers for the whole organization

Revision ID: 028_org_app_mcp
Revises: 027_drop_agent_status
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "028_org_app_mcp"
down_revision: Union[str, Sequence[str], None] = "027_drop_agent_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "org_app_mcp" in set(insp.get_table_names()):
        return
    op.create_table(
        "org_app_mcp",
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("servers", sa.JSON(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "org_app_mcp" in set(insp.get_table_names()):
        op.drop_table("org_app_mcp")
