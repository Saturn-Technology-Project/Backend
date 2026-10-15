"""Store ML-KEM-1024 payload public keys.

Revision ID: 049_mlkem_payload_keys
Revises: 048_member_assist
Create Date: 2026-10-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "049_mlkem_payload_keys"
down_revision: Union[str, Sequence[str], None] = "048_member_assist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("org_payload_keys", "public_key", existing_type=sa.String(length=64), type_=sa.Text(), existing_nullable=False)
    op.alter_column("member_payload_keys", "public_key", existing_type=sa.String(length=64), type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    op.alter_column("org_payload_keys", "public_key", existing_type=sa.Text(), type_=sa.String(length=64), existing_nullable=False)
    op.alter_column("member_payload_keys", "public_key", existing_type=sa.Text(), type_=sa.String(length=64), existing_nullable=False)
