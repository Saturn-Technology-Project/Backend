"""Remember when a member's MCP server assignments were first set.

bind_workspace used to give a member every org MCP server whenever their list was empty,
which silently undid an admin's revocation. The flag records that the list has been set
once, so an empty list afterwards stays empty.

Existing members are marked initialised: from now on their current assignments, including
an empty list an admin left them with, are kept as they are.

Revision ID: 056_org_hardening
Revises: 055_payload_keys_hardening
Create Date: 2026-10-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "056_org_hardening"
down_revision: Union[str, Sequence[str], None] = "055_payload_keys_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "mcp_assignments_initialized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(sa.text("UPDATE employees SET mcp_assignments_initialized = TRUE"))


def downgrade() -> None:
    op.drop_column("employees", "mcp_assignments_initialized")
