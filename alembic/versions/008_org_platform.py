"""organization platform fields

Revision ID: 008_org_platform
Revises: 007_agent_ws
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "008_org_platform"
down_revision: Union[str, Sequence[str], None] = "007_agent_ws"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {column["name"] for column in insp.get_columns("organizations")}
    additions = {
        "status": sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        "plan": sa.Column("plan", sa.String(length=32), nullable=False, server_default="trial"),
        "deployment_mode": sa.Column(
            "deployment_mode", sa.String(length=32), nullable=False, server_default="cloud"
        ),
        "runtime_region": sa.Column(
            "runtime_region", sa.String(length=64), nullable=False, server_default="asia-east1"
        ),
        "runtime_version": sa.Column(
            "runtime_version", sa.String(length=32), nullable=False, server_default="0.1.0"
        ),
    }
    for name, column in additions.items():
        if name not in cols:
            op.add_column("organizations", column)
    op.execute("UPDATE organizations SET plan = 'enterprise', status = 'active' WHERE id = 'org_saturn'")


def downgrade() -> None:
    op.drop_column("organizations", "runtime_version")
    op.drop_column("organizations", "runtime_region")
    op.drop_column("organizations", "deployment_mode")
    op.drop_column("organizations", "plan")
    op.drop_column("organizations", "status")
