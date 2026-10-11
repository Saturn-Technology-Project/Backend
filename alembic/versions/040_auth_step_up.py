"""Recent authentication and public auth rate limits

Revision ID: 040_auth_step_up
Revises: 039_drop_wallets_x402
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "040_auth_step_up"
down_revision: Union[str, Sequence[str], None] = "039_drop_wallets_x402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("auth_sessions")} if "auth_sessions" in inspector.get_table_names() else set()
    if "authenticated_at" not in columns:
        op.add_column("auth_sessions", sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=True))
    if "auth_throttles" not in inspector.get_table_names():
        op.create_table(
            "auth_throttles",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("bucket", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_auth_throttles_bucket", "auth_throttles", ["bucket"])
        op.create_index("ix_auth_throttles_created_at", "auth_throttles", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "auth_throttles" in inspector.get_table_names():
        op.drop_index("ix_auth_throttles_created_at", table_name="auth_throttles")
        op.drop_index("ix_auth_throttles_bucket", table_name="auth_throttles")
        op.drop_table("auth_throttles")
    columns = {item["name"] for item in inspector.get_columns("auth_sessions")} if "auth_sessions" in inspector.get_table_names() else set()
    if "authenticated_at" in columns:
        op.drop_column("auth_sessions", "authenticated_at")
