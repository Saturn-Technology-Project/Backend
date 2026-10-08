"""Store Saturn App agents apart from Saturn Cloud

Revision ID: 024_app_state
Revises: 023_pq_vault
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "024_app_state"
down_revision: Union[str, Sequence[str], None] = "023_pq_vault"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "user_app_state" in set(insp.get_table_names()):
        return
    op.create_table(
        "user_app_state",
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "user_app_state" in set(insp.get_table_names()):
        op.drop_table("user_app_state")
