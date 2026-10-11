"""Per-group approval counts

Revision ID: 042_group_requirements
Revises: 041_groups_quorum
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "042_group_requirements"
down_revision: Union[str, Sequence[str], None] = "041_groups_quorum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "approval_requirements" not in tables:
        op.create_table(
            "approval_requirements",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("approval_policy_id", sa.String(length=64), nullable=False),
            sa.Column("group_id", sa.String(length=64), nullable=False),
            sa.Column("required_count", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["approval_policy_id"], ["approval_policies.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("approval_policy_id", "group_id", name="uq_approval_requirement_group"),
        )
        op.create_index(
            "ix_approval_requirements_approval_policy_id",
            "approval_requirements",
            ["approval_policy_id"],
        )
        op.create_index("ix_approval_requirements_group_id", "approval_requirements", ["group_id"])
    if "approvals" in tables:
        columns = {item["name"] for item in inspector.get_columns("approvals")}
        if "group_requirements" not in columns:
            op.add_column(
                "approvals",
                sa.Column("group_requirements", sa.JSON(), nullable=False, server_default="[]"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "approvals" in tables:
        columns = {item["name"] for item in inspector.get_columns("approvals")}
        if "group_requirements" in columns:
            op.drop_column("approvals", "group_requirements")
    if "approval_requirements" in tables:
        op.drop_table("approval_requirements")
