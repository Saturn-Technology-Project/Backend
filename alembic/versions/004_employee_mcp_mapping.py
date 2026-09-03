"""employees, mcp servers, and user mappings

Revision ID: 004_employee_mcp_mapping
Revises: 003_users_auth
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "004_employee_mcp_mapping"
down_revision: Union[str, Sequence[str], None] = "003_users_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_mcp_servers_org_name"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_mcp_servers_organization_id",
        "mcp_servers",
        ["organization_id"],
        if_not_exists=True,
    )

    op.create_table(
        "employees",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_employees_user_id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_employees_organization_id",
        "employees",
        ["organization_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_employees_user_id",
        "employees",
        ["user_id"],
        if_not_exists=True,
    )

    op.create_table(
        "employee_agents",
        sa.Column("employee_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("employee_id", "agent_id"),
        if_not_exists=True,
    )
    op.create_table(
        "employee_mcp_servers",
        sa.Column("employee_id", sa.String(length=64), nullable=False),
        sa.Column("mcp_server_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"]),
        sa.PrimaryKeyConstraint("employee_id", "mcp_server_id"),
        if_not_exists=True,
    )
    op.create_table(
        "agent_mcp_servers",
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("mcp_server_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"]),
        sa.PrimaryKeyConstraint("agent_id", "mcp_server_id"),
        if_not_exists=True,
    )

    insp = inspect(op.get_bind())
    tool_cols = {column["name"] for column in insp.get_columns("tools")}
    if "mcp_server_id" not in tool_cols:
        op.add_column("tools", sa.Column("mcp_server_id", sa.String(length=64), nullable=True))
    tool_indexes = {index["name"] for index in insp.get_indexes("tools")}
    if "ix_tools_mcp_server_id" not in tool_indexes:
        op.create_index("ix_tools_mcp_server_id", "tools", ["mcp_server_id"])
    tool_fks = {fk["name"] for fk in insp.get_foreign_keys("tools")}
    if "fk_tools_mcp_server_id" not in tool_fks:
        op.create_foreign_key(
            "fk_tools_mcp_server_id",
            "tools",
            "mcp_servers",
            ["mcp_server_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint("fk_tools_mcp_server_id", "tools", type_="foreignkey")
    op.drop_index("ix_tools_mcp_server_id", table_name="tools")
    op.drop_column("tools", "mcp_server_id")
    op.drop_table("agent_mcp_servers")
    op.drop_table("employee_mcp_servers")
    op.drop_table("employee_agents")
    op.drop_table("employees")
    op.drop_index("ix_mcp_servers_organization_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
