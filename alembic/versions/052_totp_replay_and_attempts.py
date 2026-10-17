"""Add totp_last_step and totp_attempts to users.

Revision ID: 052_totp_replay_and_attempts
Revises: 051_admin_totp
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "052_totp_replay_and_attempts"
down_revision: Union[str, Sequence[str], None] = "051_admin_totp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("totp_attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "totp_attempts")
    op.drop_column("users", "totp_last_step")
