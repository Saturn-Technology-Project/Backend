"""Per-organization add-ons. Not shared across customers.

Revision ID: 014_org_addons
Revises: 013_agent_settings
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "014_org_addons"
down_revision: Union[str, Sequence[str], None] = "013_agent_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("organizations")}
    if "addons" not in cols:
        op.add_column(
            "organizations",
            sa.Column(
                "addons",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    op.drop_column("organizations", "addons")
