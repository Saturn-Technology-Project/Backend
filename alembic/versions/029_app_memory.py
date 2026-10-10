"""Store agent notes beside wakes

Revision ID: 029_app_memory
Revises: 028_app_wakes
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "029_app_memory"
down_revision: Union[str, Sequence[str], None] = "028_app_wakes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "memories" in columns:
        return
    op.add_column(
        "user_app_state",
        sa.Column("memories", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "memories" not in columns:
        return
    op.drop_column("user_app_state", "memories")
