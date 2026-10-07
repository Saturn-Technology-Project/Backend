"""Named MCP tool sets and remote MCP credentials

Revision ID: 018_tool_sets
Revises: 017_org_credentials
Create Date: 2026-10-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "018_tool_sets"
down_revision: Union[str, Sequence[str], None] = "017_org_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "tools" in tables and "input_schema" not in {column["name"] for column in insp.get_columns("tools")}:
        op.add_column("tools", sa.Column("input_schema", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        op.add_column("tools", sa.Column("upstream_name", sa.String(length=255), nullable=False, server_default=""))
    if "mcp_servers" in tables and "auth_ciphertext" not in {column["name"] for column in insp.get_columns("mcp_servers")}:
        op.add_column("mcp_servers", sa.Column("auth_ciphertext", sa.Text(), nullable=False, server_default=""))
    if "tool_sets" not in tables:
        op.create_table(
            "tool_sets",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "name", name="uq_tool_sets_org_name"),
        )
        op.create_index("ix_tool_sets_organization_id", "tool_sets", ["organization_id"])
    if "tool_set_items" not in tables:
        op.create_table(
            "tool_set_items",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("tool_set_id", sa.String(length=64), sa.ForeignKey("tool_sets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tool_id", sa.String(length=64), sa.ForeignKey("tools.id", ondelete="CASCADE"), nullable=False),
            sa.Column("approval", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("tool_set_id", "tool_id", name="uq_tool_set_items_tool"),
        )
        op.create_index("ix_tool_set_items_tool_set_id", "tool_set_items", ["tool_set_id"])
        op.create_index("ix_tool_set_items_tool_id", "tool_set_items", ["tool_id"])
    agent_columns = {column["name"] for column in insp.get_columns("agents")} if "agents" in tables else set()
    if "tool_set_id" not in agent_columns:
        op.add_column("agents", sa.Column("tool_set_id", sa.String(length=64), nullable=True))
        op.create_index("ix_agents_tool_set_id", "agents", ["tool_set_id"])
        op.create_foreign_key(
            "fk_agents_tool_set_id",
            "agents",
            "tool_sets",
            ["tool_set_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint("fk_agents_tool_set_id", "agents", type_="foreignkey")
    op.drop_index("ix_agents_tool_set_id", table_name="agents")
    op.drop_column("agents", "tool_set_id")
    op.drop_index("ix_tool_set_items_tool_id", table_name="tool_set_items")
    op.drop_index("ix_tool_set_items_tool_set_id", table_name="tool_set_items")
    op.drop_table("tool_set_items")
    op.drop_index("ix_tool_sets_organization_id", table_name="tool_sets")
    op.drop_table("tool_sets")
    op.drop_column("mcp_servers", "auth_ciphertext")
    op.drop_column("tools", "upstream_name")
    op.drop_column("tools", "input_schema")
