"""Record when a console session was last used, for the idle timeout.

Existing sessions start with no value and fall back to when they were last authenticated,
so a session nobody has used for a day ends at its next request.

Revision ID: 057_session_idle
Revises: 056_org_hardening
Create Date: 2026-10-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "057_session_idle"
down_revision: Union[str, Sequence[str], None] = "056_org_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("auth_sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("auth_sessions", "last_seen_at")
