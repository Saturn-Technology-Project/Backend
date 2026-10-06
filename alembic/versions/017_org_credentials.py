"""Organization model connections and API keys

Revision ID: 017_org_credentials
Revises: 016_conversation_edit
Create Date: 2026-10-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "017_org_credentials"
down_revision: Union[str, Sequence[str], None] = "016_conversation_edit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if "org_credentials" in insp.get_table_names():
        return
    op.create_table(
        "org_credentials",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("secret_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("broker_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("allowed_paths", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_org_credentials_organization_id", "org_credentials", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_org_credentials_organization_id", table_name="org_credentials")
    op.drop_table("org_credentials")
