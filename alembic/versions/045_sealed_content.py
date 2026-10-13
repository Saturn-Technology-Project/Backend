"""Drop sealed audit content after its window

Revision ID: 045_sealed_content
Revises: 044_webhooks_expiry
Create Date: 2026-10-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "045_sealed_content"
down_revision: Union[str, Sequence[str], None] = "044_webhooks_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "audit_logs" not in tables:
        return
    columns = {column["name"] for column in inspect(bind).get_columns("audit_logs")}
    if "content_until" not in columns:
        op.add_column("audit_logs", sa.Column("content_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("audit_logs")}
    if "content_until" in columns:
        op.drop_column("audit_logs", "content_until")
