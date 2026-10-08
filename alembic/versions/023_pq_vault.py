"""Move credential ciphertext into a vault and record its reference

Revision ID: 023_pq_vault
Revises: 022_mcp_oauth
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "023_pq_vault"
down_revision: Union[str, Sequence[str], None] = "022_mcp_oauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "secret_vault" not in tables:
        op.create_table(
            "secret_vault",
            sa.Column("id", sa.String(length=128), primary_key=True),
            sa.Column("ciphertext", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "mcp_servers" in tables:
        columns = {column["name"] for column in insp.get_columns("mcp_servers")}
        if "credential_id" not in columns:
            op.add_column("mcp_servers", sa.Column("credential_id", sa.String(length=128), nullable=False, server_default=""))
        if "oauth_scope" not in columns:
            op.add_column("mcp_servers", sa.Column("oauth_scope", sa.Text(), nullable=False, server_default=""))
    if "org_credentials" in tables:
        columns = {column["name"] for column in insp.get_columns("org_credentials")}
        if "vault_id" not in columns:
            op.add_column("org_credentials", sa.Column("vault_id", sa.String(length=128), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "org_credentials" in tables:
        columns = {column["name"] for column in insp.get_columns("org_credentials")}
        if "vault_id" in columns:
            op.drop_column("org_credentials", "vault_id")
    if "mcp_servers" in tables:
        columns = {column["name"] for column in insp.get_columns("mcp_servers")}
        if "oauth_scope" in columns:
            op.drop_column("mcp_servers", "oauth_scope")
        if "credential_id" in columns:
            op.drop_column("mcp_servers", "credential_id")
    if "secret_vault" in tables:
        op.drop_table("secret_vault")
