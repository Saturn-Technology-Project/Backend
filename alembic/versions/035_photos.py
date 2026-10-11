"""Add organization and user photos

Revision ID: 035_photos
Revises: 034_drop_app_sync
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "035_photos"
down_revision: Union[str, Sequence[str], None] = "034_drop_app_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {item["name"] for item in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("users", "photo"):
        op.add_column("users", sa.Column("photo", sa.Text(), nullable=True))
    if not _has_column("organizations", "photo"):
        op.add_column("organizations", sa.Column("photo", sa.Text(), nullable=True))


def downgrade() -> None:
    if _has_column("organizations", "photo"):
        op.drop_column("organizations", "photo")
    if _has_column("users", "photo"):
        op.drop_column("users", "photo")
