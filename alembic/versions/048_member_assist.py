"""Remember when a member must be helped after a password reset.

Revision ID: 048_member_assist
Revises: 047_member_key_backup
Create Date: 2026-10-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "048_member_assist"
down_revision: Union[str, Sequence[str], None] = "047_member_key_backup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("member_payload_keys")}
    if "assist_requested" in columns:
        return
    op.add_column(
        "member_payload_keys",
        sa.Column("assist_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("member_payload_keys", "assist_requested")
