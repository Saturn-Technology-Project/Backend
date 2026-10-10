"""Policy graph definition and compiled expressions

Revision ID: 032_policy_graph
Revises: 030_password_resets
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "032_policy_graph"
down_revision: Union[str, Sequence[str], None] = "030_password_resets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    policy_cols = {column["name"] for column in inspector.get_columns("policies")}
    if "tool_set_id" not in policy_cols:
        op.add_column("policies", sa.Column("tool_set_id", sa.String(length=64), nullable=True))
        op.create_index("ix_policies_tool_set_id", "policies", ["tool_set_id"])
        op.create_foreign_key(
            "fk_policies_tool_set_id",
            "policies",
            "tool_sets",
            ["tool_set_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "definition" not in policy_cols:
        op.add_column("policies", sa.Column("definition", sa.JSON(), nullable=True))
    rule_cols = {column["name"] for column in inspector.get_columns("policy_rules")}
    if "expression" not in rule_cols:
        op.add_column("policy_rules", sa.Column("expression", sa.JSON(), nullable=True))
    if "attributes" not in rule_cols:
        op.add_column("policy_rules", sa.Column("attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    rule_cols = {column["name"] for column in inspector.get_columns("policy_rules")}
    if "attributes" in rule_cols:
        op.drop_column("policy_rules", "attributes")
    if "expression" in rule_cols:
        op.drop_column("policy_rules", "expression")
    policy_cols = {column["name"] for column in inspector.get_columns("policies")}
    if "definition" in policy_cols:
        op.drop_column("policies", "definition")
    if "tool_set_id" in policy_cols:
        op.drop_constraint("fk_policies_tool_set_id", "policies", type_="foreignkey")
        op.drop_index("ix_policies_tool_set_id", table_name="policies")
        op.drop_column("policies", "tool_set_id")
