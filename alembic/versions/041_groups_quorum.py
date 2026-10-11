"""Groups and multi-member approval

Revision ID: 041_groups_quorum
Revises: 040_auth_step_up
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "041_groups_quorum"
down_revision: Union[str, Sequence[str], None] = "040_auth_step_up"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "groups" not in tables:
        op.create_table(
            "groups",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "name", name="uq_groups_org_name"),
        )
        op.create_index("ix_groups_organization_id", "groups", ["organization_id"])
    if "group_members" not in tables:
        op.create_table(
            "group_members",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("group_id", sa.String(length=64), nullable=False),
            sa.Column("employee_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("group_id", "employee_id", name="uq_group_members"),
        )
        op.create_index("ix_group_members_group_id", "group_members", ["group_id"])
        op.create_index("ix_group_members_employee_id", "group_members", ["employee_id"])
    if "approval_policies" not in tables:
        op.create_table(
            "approval_policies",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("required_count", sa.Integer(), nullable=False),
            sa.Column("group_id", sa.String(length=64), nullable=True),
            sa.Column("allow_requester", sa.Boolean(), nullable=False),
            sa.Column("expires_minutes", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_approval_policies_organization_id", "approval_policies", ["organization_id"])
    if "approval_decisions" not in tables:
        op.create_table(
            "approval_decisions",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("approval_id", sa.String(length=64), nullable=False),
            sa.Column("employee_id", sa.String(length=64), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("approval_id", "employee_id", name="uq_approval_vote"),
        )
        op.create_index("ix_approval_decisions_approval_id", "approval_decisions", ["approval_id"])
        op.create_index("ix_approval_decisions_employee_id", "approval_decisions", ["employee_id"])
    columns = {item["name"] for item in inspector.get_columns("approvals")} if "approvals" in tables else set()
    additions = {
        "required_count": sa.Column("required_count", sa.Integer(), nullable=False, server_default="1"),
        "approval_policy_id": sa.Column("approval_policy_id", sa.String(length=64), nullable=True),
        "group_id": sa.Column("group_id", sa.String(length=64), nullable=True),
        "allow_requester": sa.Column("allow_requester", sa.Boolean(), nullable=False, server_default=sa.true()),
        "parameter_digest": sa.Column("parameter_digest", sa.String(length=64), nullable=True),
        "policy_id": sa.Column("policy_id", sa.String(length=64), nullable=True),
        "policy_version": sa.Column("policy_version", sa.Integer(), nullable=True),
        "approval_policy_version": sa.Column("approval_policy_version", sa.Integer(), nullable=True),
        "expires_at": sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        "execution_state": sa.Column("execution_state", sa.String(length=32), nullable=False, server_default="none"),
        "requester_user_id": sa.Column("requester_user_id", sa.String(length=64), nullable=True),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("approvals", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "approvals" in tables:
        columns = {item["name"] for item in inspector.get_columns("approvals")}
        for name in (
            "requester_user_id",
            "execution_state",
            "expires_at",
            "approval_policy_version",
            "policy_version",
            "policy_id",
            "parameter_digest",
            "allow_requester",
            "group_id",
            "approval_policy_id",
            "required_count",
        ):
            if name in columns:
                op.drop_column("approvals", name)
    if "approval_decisions" in tables:
        op.drop_table("approval_decisions")
    if "approval_policies" in tables:
        op.drop_table("approval_policies")
    if "group_members" in tables:
        op.drop_table("group_members")
    if "groups" in tables:
        op.drop_table("groups")
