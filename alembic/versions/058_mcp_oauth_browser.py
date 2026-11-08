"""Bind each MCP sign-in to the browser that started it.

The callback only knew the sign-in by its state, so a link someone else opened finished the
sign-in for whoever started it. A pending sign-in now keeps the hash of a secret held in a
cookie on the starting browser. Rows from before have no hash and are refused, which only
means a sign-in started during the deploy has to be started again.

Revision ID: 058_mcp_oauth_browser
Revises: 057_session_idle
Create Date: 2026-11-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "058_mcp_oauth_browser"
down_revision: Union[str, Sequence[str], None] = "057_session_idle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_oauth_pending",
        sa.Column("browser_hash", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("mcp_oauth_pending", "browser_hash")
