"""Atomic per-organization audit numbers.

Revision ID: 050_audit_counters
Revises: 049_mlkem_payload_keys
Create Date: 2026-10-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "050_audit_counters"
down_revision: Union[str, Sequence[str], None] = "049_mlkem_payload_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_counters",
        sa.Column("organization_id", sa.String(length=64), primary_key=True),
        sa.Column("event", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_counters")
