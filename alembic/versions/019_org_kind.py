"""Personal and organization accounts

Revision ID: 019_org_kind
Revises: 018_tool_sets
Create Date: 2026-10-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "019_org_kind"
down_revision: Union[str, Sequence[str], None] = "018_tool_sets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "organizations" not in set(insp.get_table_names()):
        return
    columns = {column["name"] for column in insp.get_columns("organizations")}
    if "kind" not in columns:
        op.add_column(
            "organizations",
            sa.Column("kind", sa.String(length=32), nullable=False, server_default="personal"),
        )
    op.execute("UPDATE organizations SET plan = 'free' WHERE plan IN ('trial', '')")
    op.execute("UPDATE organizations SET plan = 'pro' WHERE plan = 'starter'")
    op.execute("UPDATE organizations SET kind = 'organization' WHERE plan = 'enterprise'")
    op.execute("UPDATE organizations SET kind = 'personal' WHERE plan IN ('free', 'pro')")


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "organizations" not in set(insp.get_table_names()):
        return
    columns = {column["name"] for column in insp.get_columns("organizations")}
    if "kind" in columns:
        op.drop_column("organizations", "kind")
