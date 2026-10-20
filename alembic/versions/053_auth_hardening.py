"""Scope OAuth consent sessions and make TOTP lockouts expire.

Revision ID: 053_auth_hardening
Revises: 052_totp_replay_and_attempts
Create Date: 2026-10-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "053_auth_hardening"
down_revision: Union[str, Sequence[str], None] = "052_totp_replay_and_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions",
        sa.Column("purpose", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "users",
        sa.Column("totp_locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    # A user locked out before this change had no way back. Give them a fresh window.
    op.execute("UPDATE users SET totp_attempts = 0 WHERE totp_attempts >= 5")


def downgrade() -> None:
    op.drop_column("users", "totp_locked_until")
    op.drop_column("auth_sessions", "purpose")
