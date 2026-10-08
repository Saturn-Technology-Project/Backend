"""Store team agents for the whole organization

Revision ID: 025_org_app_state
Revises: 024_app_state
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "025_org_app_state"
down_revision: Union[str, Sequence[str], None] = "024_app_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "org_app_state" in set(insp.get_table_names()):
        return
    op.create_table(
        "org_app_state",
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("agents", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "org_app_state" in set(insp.get_table_names()):
        op.drop_table("org_app_state")
