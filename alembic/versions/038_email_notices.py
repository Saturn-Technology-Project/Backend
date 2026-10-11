"""Profile email notices for approval, execution, and audit

Revision ID: 038_email_notices
Revises: 037_login_mfa
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "038_email_notices"
down_revision: Union[str, Sequence[str], None] = "037_login_mfa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {item["name"] for item in inspect(bind).get_columns(table)}


def upgrade() -> None:
    for name in ("notify_approval", "notify_execute", "notify_audit"):
        if not _has_column("users", name):
            op.add_column(
                "users",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
            )
    if not _has_column("users", "locale"):
        op.add_column(
            "users",
            sa.Column("locale", sa.String(length=16), nullable=False, server_default="zh-Hant"),
        )


def downgrade() -> None:
    if _has_column("users", "locale"):
        op.drop_column("users", "locale")
    for name in ("notify_audit", "notify_execute", "notify_approval"):
        if _has_column("users", name):
            op.drop_column("users", name)
