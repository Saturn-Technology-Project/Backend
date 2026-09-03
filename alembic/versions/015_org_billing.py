"""Organization billing snapshot for the subscription console

Revision ID: 015_org_billing
Revises: 014_org_addons
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "015_org_billing"
down_revision: Union[str, Sequence[str], None] = "014_org_addons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("organizations")}
    if "billing" not in cols:
        op.add_column(
            "organizations",
            sa.Column(
                "billing",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    op.drop_column("organizations", "billing")
