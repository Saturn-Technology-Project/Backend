"""Password-wrapped member private key backups.

Revision ID: 047_member_key_backup
Revises: 046_payload_zero_access
Create Date: 2026-10-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "047_member_key_backup"
down_revision: Union[str, Sequence[str], None] = "046_payload_zero_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "member_key_backups" in set(inspect(bind).get_table_names()):
        return
    op.create_table(
        "member_key_backups",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("member_key_id", sa.String(length=64), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("kdf", sa.String(length=64), nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column("salt", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_member_key_backups_organization_id", "member_key_backups", ["organization_id"])
    op.create_index("ix_member_key_backups_user_id", "member_key_backups", ["user_id"])
    op.create_index("ix_member_key_backups_member_key_id", "member_key_backups", ["member_key_id"])


def downgrade() -> None:
    op.drop_table("member_key_backups")
