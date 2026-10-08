"""Drop the agent active/paused status

Revision ID: 027_drop_agent_status
Revises: 026_drop_connector
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "027_drop_agent_status"
down_revision: Union[str, Sequence[str], None] = "026_drop_connector"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "agents" not in set(insp.get_table_names()):
        return
    columns = {column["name"] for column in insp.get_columns("agents")}
    if "status" in columns:
        op.drop_column("agents", "status")


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "agents" not in set(insp.get_table_names()):
        return
    columns = {column["name"] for column in insp.get_columns("agents")}
    if "status" not in columns:
        op.add_column(
            "agents",
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        )
