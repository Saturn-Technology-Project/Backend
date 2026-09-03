"""policy product module: versions, structured rules, agent bindings

Revision ID: 011_policy_module
Revises: 010_enrollment_token
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "011_policy_module"
down_revision: Union[str, Sequence[str], None] = "010_enrollment_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    policy_cols = {column["name"] for column in insp.get_columns("policies")}
    rule_cols = {column["name"] for column in insp.get_columns("policy_rules")}
    action_cols = {column["name"] for column in insp.get_columns("actions")}

    if "description" not in policy_cols:
        op.add_column(
            "policies",
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
        )
    if "status" not in policy_cols:
        op.add_column(
            "policies",
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        )
    if "version" not in policy_cols:
        op.add_column(
            "policies",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
    if "environment" not in policy_cols:
        op.add_column(
            "policies",
            sa.Column(
                "environment",
                sa.String(length=64),
                nullable=False,
                server_default="production",
            ),
        )
    if "updated_at" not in policy_cols:
        op.add_column(
            "policies",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute("UPDATE policies SET updated_at = created_at WHERE updated_at IS NULL")

    if "conditions" not in rule_cols:
        op.add_column("policy_rules", sa.Column("conditions", sa.JSON(), nullable=True))

    if "policy_id" not in action_cols:
        op.add_column("actions", sa.Column("policy_id", sa.String(length=64), nullable=True))
    if "policy_rule_id" not in action_cols:
        op.add_column(
            "actions", sa.Column("policy_rule_id", sa.String(length=64), nullable=True)
        )
    if "policy_version" not in action_cols:
        op.add_column("actions", sa.Column("policy_version", sa.Integer(), nullable=True))
    if "policy_reason" not in action_cols:
        op.add_column(
            "actions", sa.Column("policy_reason", sa.String(length=512), nullable=True)
        )

    tables = set(insp.get_table_names())
    if "agent_policies" not in tables:
        op.create_table(
            "agent_policies",
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("policy_id", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
            sa.ForeignKeyConstraint(["policy_id"], ["policies.id"]),
            sa.PrimaryKeyConstraint("agent_id", "policy_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "agent_policies" in set(insp.get_table_names()):
        op.drop_table("agent_policies")
    action_cols = {column["name"] for column in insp.get_columns("actions")}
    for column in ("policy_reason", "policy_version", "policy_rule_id", "policy_id"):
        if column in action_cols:
            op.drop_column("actions", column)
    rule_cols = {column["name"] for column in insp.get_columns("policy_rules")}
    if "conditions" in rule_cols:
        op.drop_column("policy_rules", "conditions")
    policy_cols = {column["name"] for column in insp.get_columns("policies")}
    for column in ("updated_at", "environment", "version", "status", "description"):
        if column in policy_cols:
            op.drop_column("policies", column)
