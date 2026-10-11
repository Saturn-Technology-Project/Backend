"""Remember which task notices were already sent

Revision ID: 043_notice_deliveries
Revises: 042_group_requirements
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "043_notice_deliveries"
down_revision: Union[str, Sequence[str], None] = "042_group_requirements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "notice_deliveries" in tables:
        return
    op.create_table(
        "notice_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", "idempotency_key", name="uq_notice_delivery"),
    )
    op.create_index("ix_notice_deliveries_organization_id", "notice_deliveries", ["organization_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "notice_deliveries" not in tables:
        return
    op.drop_index("ix_notice_deliveries_organization_id", table_name="notice_deliveries")
    op.drop_table("notice_deliveries")
