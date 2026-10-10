"""Store agent wakes beside durable tasks

Revision ID: 028_app_wakes
Revises: 027_app_tasks
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "028_app_wakes"
down_revision: Union[str, Sequence[str], None] = "027_app_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "wakes" in columns:
        return
    op.add_column(
        "user_app_state",
        sa.Column("wakes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "wakes" not in columns:
        return
    op.drop_column("user_app_state", "wakes")
