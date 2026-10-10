"""Password reset codes

Revision ID: 030_password_resets
Revises: 029_app_memory
Create Date: 2026-10-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "030_password_resets"
down_revision: Union[str, Sequence[str], None] = "029_app_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "password_resets" in inspect(bind).get_table_names():
        return
    op.create_table(
        "password_resets",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_password_resets_email", "password_resets", ["email"])


def downgrade() -> None:
    bind = op.get_bind()
    if "password_resets" not in inspect(bind).get_table_names():
        return
    op.drop_index("ix_password_resets_email", table_name="password_resets")
    op.drop_table("password_resets")
