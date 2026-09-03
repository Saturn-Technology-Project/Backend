"""persist current agent enrollment token

Revision ID: 010_enrollment_token
Revises: 009_remove_demo_catalog
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "010_enrollment_token"
down_revision: Union[str, Sequence[str], None] = "009_remove_demo_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("agents")}
    if "enrollment_token" not in cols:
        op.add_column(
            "agents",
            sa.Column("enrollment_token", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("agents", "enrollment_token")
