"""Drop the connector socket and the old MCP secret columns

Revision ID: 026_drop_connector
Revises: 025_org_app_state
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "026_drop_connector"
down_revision: Union[str, Sequence[str], None] = "025_org_app_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set[str]:
    if table not in set(insp.get_table_names()):
        return set()
    return {column["name"] for column in insp.get_columns(table)}


def _drop_fk(table: str, column: str) -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    for fk in insp.get_foreign_keys(table):
        if column in (fk.get("constrained_columns") or []):
            op.drop_constraint(fk["name"], table, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    agent_cols = _columns(insp, "agents")
    if "connector_id" in agent_cols:
        _drop_fk("agents", "connector_id")
        op.drop_index("ix_agents_connector_id", table_name="agents")
        op.drop_column("agents", "connector_id")
    if "token_hash" in agent_cols:
        op.drop_index("ix_agents_token_hash", table_name="agents")
        op.drop_column("agents", "token_hash")
    if "enrollment_token" in agent_cols:
        op.drop_column("agents", "enrollment_token")
    mcp_cols = _columns(insp, "mcp_servers")
    if "connector_id" in mcp_cols:
        _drop_fk("mcp_servers", "connector_id")
        op.drop_index("ix_mcp_servers_connector_id", table_name="mcp_servers")
        op.drop_column("mcp_servers", "connector_id")
    for column in ("auth_ciphertext", "refresh_ciphertext", "oauth_client_secret"):
        if column in mcp_cols:
            op.drop_column("mcp_servers", column)
    if "connectors" in set(insp.get_table_names()):
        op.drop_table("connectors")


def downgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("enrollment_token", sa.String(length=255), nullable=True),
        sa.Column("environment_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("agents", sa.Column("connector_id", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("token_hash", sa.String(length=64), nullable=True))
    op.add_column("agents", sa.Column("enrollment_token", sa.String(length=255), nullable=True))
    op.add_column("mcp_servers", sa.Column("connector_id", sa.String(length=64), nullable=True))
    op.add_column("mcp_servers", sa.Column("auth_ciphertext", sa.Text(), nullable=False, server_default=""))
    op.add_column("mcp_servers", sa.Column("refresh_ciphertext", sa.Text(), nullable=False, server_default=""))
    op.add_column("mcp_servers", sa.Column("oauth_client_secret", sa.Text(), nullable=False, server_default=""))
