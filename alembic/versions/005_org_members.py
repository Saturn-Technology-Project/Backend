"""org member roles, permissions, and invitations

Revision ID: 005_org_members
Revises: 004_employee_mcp_mapping
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "005_org_members"
down_revision: Union[str, Sequence[str], None] = "004_employee_mcp_mapping"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    employee_cols = {column["name"] for column in insp.get_columns("employees")}
    if "role" not in employee_cols:
        op.add_column(
            "employees",
            sa.Column("role", sa.String(length=32), nullable=False, server_default="operator"),
        )
    if "permissions" not in employee_cols:
        op.add_column(
            "employees",
            sa.Column("permissions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )

    op.create_table(
        "invitations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("invited_by_user_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("agent_ids", sa.JSON(), nullable=False),
        sa.Column("mcp_server_ids", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_invitations_organization_id",
        "invitations",
        ["organization_id"],
        if_not_exists=True,
    )
    op.create_index("ix_invitations_email", "invitations", ["email"], if_not_exists=True)
    op.create_index("ix_invitations_status", "invitations", ["status"], if_not_exists=True)
    op.create_index(
        "ix_invitations_token_hash",
        "invitations",
        ["token_hash"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("invitations")
    op.drop_column("employees", "permissions")
    op.drop_column("employees", "role")
