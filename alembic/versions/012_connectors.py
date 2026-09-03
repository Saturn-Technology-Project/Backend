"""Saturn Connector: org-level enrollment and catalog registration

Revision ID: 012_connectors
Revises: 011_policy_module
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "012_connectors"
down_revision: Union[str, Sequence[str], None] = "011_policy_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "connectors" not in tables:
        op.create_table(
            "connectors",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("environment_id", sa.String(length=64), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("capabilities", sa.JSON(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=True),
            sa.Column("enrollment_token", sa.String(length=255), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.UniqueConstraint("organization_id", name="uq_connectors_organization_id"),
        )
        op.create_index("ix_connectors_organization_id", "connectors", ["organization_id"])
        op.create_index("ix_connectors_token_hash", "connectors", ["token_hash"])

    agent_cols = {column["name"] for column in insp.get_columns("agents")}
    if "connector_id" not in agent_cols:
        op.add_column("agents", sa.Column("connector_id", sa.String(length=64), nullable=True))
        op.create_foreign_key(
            "fk_agents_connector_id",
            "agents",
            "connectors",
            ["connector_id"],
            ["id"],
        )
        op.create_index("ix_agents_connector_id", "agents", ["connector_id"])
    if "runtime_id" not in agent_cols:
        op.add_column("agents", sa.Column("runtime_id", sa.String(length=64), nullable=True))

    mcp_cols = {column["name"] for column in insp.get_columns("mcp_servers")}
    if "connector_id" not in mcp_cols:
        op.add_column(
            "mcp_servers",
            sa.Column("connector_id", sa.String(length=64), nullable=True),
        )
        op.create_foreign_key(
            "fk_mcp_servers_connector_id",
            "mcp_servers",
            "connectors",
            ["connector_id"],
            ["id"],
        )
        op.create_index("ix_mcp_servers_connector_id", "mcp_servers", ["connector_id"])


def downgrade() -> None:
    op.drop_constraint("fk_mcp_servers_connector_id", "mcp_servers", type_="foreignkey")
    op.drop_index("ix_mcp_servers_connector_id", table_name="mcp_servers")
    op.drop_column("mcp_servers", "connector_id")
    op.drop_constraint("fk_agents_connector_id", "agents", type_="foreignkey")
    op.drop_index("ix_agents_connector_id", table_name="agents")
    op.drop_column("agents", "connector_id")
    op.drop_column("agents", "runtime_id")
    op.drop_index("ix_connectors_token_hash", table_name="connectors")
    op.drop_index("ix_connectors_organization_id", table_name="connectors")
    op.drop_table("connectors")
