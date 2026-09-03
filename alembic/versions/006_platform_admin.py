"""platform admin flag on users

Revision ID: 006_platform_admin
Revises: 005_org_members
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "006_platform_admin"
down_revision: Union[str, Sequence[str], None] = "005_org_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("users")}
    if "is_platform_admin" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "is_platform_admin",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    op.execute("UPDATE users SET is_platform_admin = true WHERE id = 'user_operator'")


def downgrade() -> None:
    op.drop_column("users", "is_platform_admin")
