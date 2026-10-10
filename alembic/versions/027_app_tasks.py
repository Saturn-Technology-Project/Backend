"""Store durable agent tasks beside synced conversations

Revision ID: 027_app_tasks
Revises: 028_org_app_mcp
Create Date: 2026-10-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "027_app_tasks"
down_revision: Union[str, Sequence[str], None] = "028_org_app_mcp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "tasks" in columns:
        return
    op.add_column(
        "user_app_state",
        sa.Column("tasks", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("user_app_state")}
    if "tasks" not in columns:
        return
    op.drop_column("user_app_state", "tasks")
