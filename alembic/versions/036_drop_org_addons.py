"""Drop organization add-ons

Revision ID: 036_drop_org_addons
Revises: 035_photos
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "036_drop_org_addons"
down_revision: Union[str, Sequence[str], None] = "035_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("organizations")}
    if "addons" in columns:
        op.drop_column("organizations", "addons")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("organizations")}
    if "addons" not in columns:
        op.add_column("organizations", sa.Column("addons", sa.JSON(), nullable=False, server_default="[]"))
