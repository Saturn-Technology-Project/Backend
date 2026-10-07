"""OAuth clients that agents use to register with Saturn MCP

Revision ID: 021_oauth_clients
Revises: 020_user_app_mcp
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "021_oauth_clients"
down_revision: Union[str, Sequence[str], None] = "020_user_app_mcp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "oauth_clients" not in tables:
        op.create_table(
            "oauth_clients",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("redirect_uris", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "oauth_grants" not in tables:
        op.create_table(
            "oauth_grants",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("client_id", sa.String(length=64), sa.ForeignKey("oauth_clients.id"), nullable=False),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("code_challenge", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_oauth_grants_code_hash", "oauth_grants", ["code_hash"], unique=True)
        op.create_index("ix_oauth_grants_client_id", "oauth_grants", ["client_id"])
        op.create_index("ix_oauth_grants_user_id", "oauth_grants", ["user_id"])
    if "oauth_access_tokens" not in tables:
        op.create_table(
            "oauth_access_tokens",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("access_hash", sa.String(length=64), nullable=False),
            sa.Column("refresh_hash", sa.String(length=64), nullable=False),
            sa.Column("client_id", sa.String(length=64), sa.ForeignKey("oauth_clients.id"), nullable=False),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_oauth_access_tokens_access_hash", "oauth_access_tokens", ["access_hash"], unique=True)
        op.create_index("ix_oauth_access_tokens_refresh_hash", "oauth_access_tokens", ["refresh_hash"], unique=True)
        op.create_index("ix_oauth_access_tokens_client_id", "oauth_access_tokens", ["client_id"])
        op.create_index("ix_oauth_access_tokens_user_id", "oauth_access_tokens", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "oauth_access_tokens" in tables:
        op.drop_table("oauth_access_tokens")
    if "oauth_grants" in tables:
        op.drop_table("oauth_grants")
    if "oauth_clients" in tables:
        op.drop_table("oauth_clients")
