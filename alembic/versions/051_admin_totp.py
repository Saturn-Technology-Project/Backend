"""Store admin TOTP secrets and hashed backup codes.

Revision ID: 051_admin_totp
Revises: 050_audit_counters
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "051_admin_totp"
down_revision: Union[str, Sequence[str], None] = "050_audit_counters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("totp_secret", sa.Text(), nullable=False, server_default=""))
    op.create_table(
        "totp_backup_codes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("totp_backup_codes")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "totp_enabled")
