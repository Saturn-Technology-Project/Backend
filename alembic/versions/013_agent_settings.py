"""Agent configuration settings JSON

Revision ID: 013_agent_settings
Revises: 012_connectors
Create Date: 2026-09-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "013_agent_settings"
down_revision: Union[str, Sequence[str], None] = "012_connectors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    agent_cols = {column["name"] for column in insp.get_columns("agents")}
    if "settings" not in agent_cols:
        op.add_column(
            "agents",
            sa.Column(
                "settings",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    op.drop_column("agents", "settings")
