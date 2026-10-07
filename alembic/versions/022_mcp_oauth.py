"""Store marketplace MCP sign-in until the provider redirects back

Revision ID: 022_mcp_oauth
Revises: 021_oauth_clients
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "022_mcp_oauth"
down_revision: Union[str, Sequence[str], None] = "021_oauth_clients"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "mcp_servers" in tables:
        columns = {column["name"] for column in insp.get_columns("mcp_servers")}
        additions = {
            "refresh_ciphertext": sa.Column("refresh_ciphertext", sa.Text(), nullable=False, server_default=""),
            "token_expires_at": sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
            "oauth_client_id": sa.Column("oauth_client_id", sa.Text(), nullable=False, server_default=""),
            "oauth_client_secret": sa.Column("oauth_client_secret", sa.Text(), nullable=False, server_default=""),
            "oauth_token_endpoint": sa.Column("oauth_token_endpoint", sa.Text(), nullable=False, server_default=""),
            "oauth_resource": sa.Column("oauth_resource", sa.Text(), nullable=False, server_default=""),
        }
        for name, column in additions.items():
            if name not in columns:
                op.add_column("mcp_servers", column)
    if "mcp_oauth_pending" not in tables:
        op.create_table(
            "mcp_oauth_pending",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column("nonce", sa.String(length=128), nullable=False),
            sa.Column("verifier_ciphertext", sa.Text(), nullable=False),
            sa.Column("client_id", sa.Text(), nullable=False),
            sa.Column("client_secret_ciphertext", sa.Text(), nullable=False, server_default=""),
            sa.Column("token_endpoint", sa.Text(), nullable=False),
            sa.Column("resource", sa.Text(), nullable=False, server_default=""),
            sa.Column("scope", sa.Text(), nullable=False, server_default=""),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_mcp_oauth_pending_state_hash", "mcp_oauth_pending", ["state_hash"], unique=True)
        op.create_index("ix_mcp_oauth_pending_user_id", "mcp_oauth_pending", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "mcp_oauth_pending" in tables:
        op.drop_table("mcp_oauth_pending")
    if "mcp_servers" in tables:
        columns = {column["name"] for column in insp.get_columns("mcp_servers")}
        for name in (
            "oauth_resource",
            "oauth_token_endpoint",
            "oauth_client_secret",
            "oauth_client_id",
            "token_expires_at",
            "refresh_ciphertext",
        ):
            if name in columns:
                op.drop_column("mcp_servers", name)
